import logging

from markupsafe import Markup
from psycopg2 import IntegrityError

from odoo import fields, models
from odoo.addons.whatsmeow.models.whatsmeow_markup import render_markup
from odoo.addons.whatsmeow.models.whatsmeow_match_mixin import _phone_tail

_logger = logging.getLogger(__name__)


class WhatsmeowMessage(models.Model):
    _inherit = "whatsmeow.message"

    mail_message_id = fields.Many2one(
        "mail.message", string="Discuss Message", index=True, ondelete="set null",
        help="The Discuss bubble mirroring this WhatsApp message — the inbound "
             "post for a received message, the operator's reply for a sent one. "
             "Lets delivery state find the bubble to annotate.",
    )

    def _deliver_inbound(self):
        """Route an accepted inbound message into its Discuss conversation when
        the session opts in; otherwise fall back to the core chatter post."""
        self.ensure_one()
        if not self.session_id.route_to_discuss:
            return super()._deliver_inbound()
        channel = self._wa_get_or_create_channel()
        self._wa_post_into_channel(channel)

    # -- reactions ------------------------------------------------------------
    def _apply_reaction(self, emoji, partner):
        """React on this message's Discuss bubble, mirroring WhatsApp's
        one-reaction-per-sender rule: a new emoji replaces the sender's previous
        one, and an empty emoji clears it. Applied under `whatsmeow_skip_send`
        so it is not echoed straight back out to WhatsApp."""
        self.ensure_one()
        bubble = self.mail_message_id
        if not bubble:
            return  # not a routed conversation — no bubble to annotate
        channel = (self.env["discuss.channel"].browse(bubble.res_id)
                   if bubble.model == "discuss.channel"
                   else self.env["discuss.channel"])
        # A private chat's reactor may be unknown (LID-only); fall back to the
        # conversation's correspondent. A group reactor with no partner can't be
        # attributed, so it is dropped.
        reactor = partner or channel.whatsmeow_partner_id
        if not reactor:
            return
        guest = self.env["mail.guest"]
        bubble = bubble.sudo().with_context(whatsmeow_skip_send=True)
        current = self.env["mail.message.reaction"].sudo().search([
            ("message_id", "=", bubble.id), ("partner_id", "=", reactor.id),
        ]).mapped("content")
        for content in current:
            if content != emoji:
                bubble._message_reaction(content, "remove", reactor, guest)
        if emoji and emoji not in current:
            bubble._message_reaction(emoji, "add", reactor, guest)

    def _send_reaction(self, emoji):
        """Send (or clear, when emoji is empty) a WhatsApp reaction on this
        message. Inline like a reply — a reaction is a live gesture, not queued
        traffic. A gateway failure is logged, never raised into the operator's
        own reaction."""
        self.ensure_one()
        if not self.wa_message_id:
            return
        payload = {
            **self._target_payload(),
            "target_id": self.wa_message_id,
            # who authored the target: the participant for an inbound message;
            # for our own outbound message the gateway uses the session's JID.
            "target_sender": self.sender_jid or "",
            "from_me": self.direction == "out",
            "emoji": emoji,
        }
        try:
            self.session_id._gw(
                "POST", f"/sessions/{self.session_id.code}/react", payload)
        except Exception as exc:  # noqa: BLE001 - a failed reaction must not raise
            _logger.warning("whatsmeow: reaction relay for message %s failed: %s",
                            self.id, exc)

    # -- routing facts (from the stored record, so media delivery works too) --
    def _wa_route_facts(self):
        self.ensure_one()
        return {
            "chat_type": self.chat_type,
            "message_type": self.message_type,
            "partner_id": self.partner_id.id or False,
            "sender_state": "existing" if self.partner_id else "new",
            "chat_jid": self.chat_jid or "",
            "phone_tail": _phone_tail(self.phone or ""),
            "sender_lid": self.sender_lid or "",
            "body": (self.body or "").lower(),
            "is_placeholder": self.is_placeholder,
        }

    # -- correspondent & naming ----------------------------------------------
    def _wa_correspondent_partner(self):
        """The partner a private conversation is with. A group has no single
        correspondent (each bubble is authored by its participant). A LID-only
        sender has no phone and cannot be created meaningfully, so it falls
        back to a generic (empty) correspondent."""
        self.ensure_one()
        if self.chat_type == "group":
            return self.env["res.partner"]
        if self.partner_id:
            return self.partner_id
        if not self.session_id.auto_create_partner or not self.phone:
            return self.env["res.partner"]
        partner = self.env["res.partner"].sudo().create({
            "name": self.push_name or self.phone,
            "phone": self.phone,
        })
        # The message that opened the conversation was created before this
        # partner existed, so its partner_id is still blank — later messages
        # resolve to the partner by phone, but this first one would not. Pin it
        # now so the log and chatter agree from the very first message.
        self.partner_id = partner
        return partner

    def _wa_display_phone(self):
        """The correspondent's number as a person would write it. WhatsApp
        hands us bare digits; the leading + is what makes them read as a phone
        number rather than an id."""
        self.ensure_one()
        phone = (self.phone or "").strip()
        if phone and not phone.startswith("+"):
            phone = "+" + phone
        return phone

    def _wa_display_name(self):
        """The correspondent's name, or nothing when all we have is a number.

        An auto-created contact is named after the number when WhatsApp gave us
        no push name, so a bare `name` is not proof we know who this is —
        compare it to the digits before treating it as a name.
        """
        self.ensure_one()
        name = (self.partner_id.name or self.push_name or "").strip()
        digits = _phone_tail(name)
        if name and digits and digits == _phone_tail(self.phone or ""):
            return ""  # "919..." is the number over again, not a name
        return name

    def _wa_conversation_label(self):
        """What to call this conversation: the name *and* the number.

        Either alone is a worse answer than both. A name with no number leaves
        an operator guessing which of three Fasils is on the line and unable to
        cross-check the contact record; a number with no name is unreadable in
        a sidebar. A group has one subject and no number at all.
        """
        self.ensure_one()
        if self.chat_type == "group":
            return self.chat_name or self.chat_jid or self.env._("WhatsApp Group")
        name, phone = self._wa_display_name(), self._wa_display_phone()
        if name and phone:
            return f"{name} ({phone})"
        return (name or phone or self.sender_lid or self.chat_jid
                or self.env._("WhatsApp"))

    def _wa_channel_name(self, partner):
        self.ensure_one()
        return self._wa_conversation_label()

    # -- find or create the conversation's channel ---------------------------
    def _wa_get_or_create_channel(self):
        self.ensure_one()
        session = self.session_id
        Channel = self.env["discuss.channel"].sudo()
        domain = [
            ("channel_type", "=", "whatsmeow"),
            ("whatsmeow_session_id", "=", session.id),
            ("whatsmeow_chat_jid", "=", self.chat_jid or ""),
        ]
        channel = Channel.search(domain, limit=1)
        if channel:
            self._wa_refresh_channel_identity(channel)
            return channel

        # First message of a conversation: route, *then* resolve the
        # correspondent. Order matters — `_wa_correspondent_partner` may
        # auto-create and pin `partner_id`, which would flip the sender from
        # "new" to "existing"; routing must see the sender as it arrived.
        operators = session._route_users(self._wa_route_facts())
        operator_pids = operators.partner_id.ids
        partner = self._wa_correspondent_partner()
        vals = {
            "name": self._wa_channel_name(partner),
            "channel_type": "whatsmeow",
            "whatsmeow_session_id": session.id,
            "whatsmeow_chat_jid": self.chat_jid or "",
            "whatsmeow_partner_id": partner.id or False,
            "channel_member_ids": [(0, 0, {"partner_id": pid}) for pid in operator_pids],
        }
        try:
            with self.env.cr.savepoint():
                channel = Channel.create(vals)
                channel.flush_recordset()
        except IntegrityError:
            # Two inbound webhooks raced; the unique index settled it. Whoever
            # lost just reuses the winner's channel.
            _logger.info("whatsmeow: concurrent channel for %s settled by the "
                         "database", self.chat_jid)
            return Channel.search(domain, limit=1)

        # discuss.channel.create always adds the acting user (here the webhook's
        # sudo user) as a member. Trim membership back to exactly the routed
        # operators, so "member == attending operator" holds — including the
        # empty case (a conversation nobody was routed to).
        extra = channel.channel_member_ids.filtered(
            lambda m: m.partner_id.id not in operator_pids)
        if extra:
            extra.unlink()
        return channel

    def _wa_refresh_channel_identity(self, channel):
        """Let a conversation learn who it is with, after the fact.

        A conversation often opens before we know anything: WhatsApp's first
        copy of a message can be an empty placeholder, a push name arrives on
        the second message, and the contact may be created in Odoo days later.
        The channel keeps whatever it was told at create, so an operator is left
        looking at a bare number forever.

        Only ever an *upgrade*, and only over a label we generated ourselves: a
        channel someone has renamed by hand, or one that already carries a name,
        is left exactly as it is.
        """
        self.ensure_one()
        if self.chat_type == "group":
            return
        vals = {}
        # A correspondent we could not resolve at create (a placeholder, or a
        # contact made later) — the author of every past bubble stays as it was,
        # but from here on the conversation has a face.
        if not channel.whatsmeow_partner_id and self.partner_id:
            vals["whatsmeow_partner_id"] = self.partner_id.id
        label = self._wa_conversation_label()
        if label and label != channel.name and self._wa_display_name():
            # Was the current name one of ours, built from nothing but the
            # number? Then it is safe to replace; anything else is somebody's
            # deliberate choice.
            generated = {self._wa_display_phone(), self.phone or "",
                         self.sender_lid or "", self.chat_jid or ""}
            if (channel.name or "") in generated:
                vals["name"] = label
        if vals:
            channel.sudo().write(vals)

    # -- post the inbound message as a bubble --------------------------------
    def _wa_bubble_author_label(self, channel):
        """What to write above a bubble that has no Odoo author.

        A LID-only sender, or a group participant we have never met, resolves to
        no `res.partner`, and Discuss renders an authorless bubble as "Unnamed" —
        which tells an operator nothing and looks broken next to a channel that
        is named after the very person who wrote. `email_from` is Discuss's own
        documented stand-in for an author it has no record of ("replaces the
        author_id field in the chatter"), and the client falls back to it before
        it gives up and says Unnamed.
        """
        self.ensure_one()
        name, phone = self._wa_display_name(), self._wa_display_phone()
        if name and phone:
            return f"{name} ({phone})"
        return (name or phone or channel.whatsmeow_partner_id.name
                or self.sender_lid or self.sender_jid or self.env._("WhatsApp"))

    def _wa_post_into_channel(self, channel):
        self.ensure_one()
        if not channel:
            return
        attachments = self.env["ir.attachment"]
        if self.media_data:
            attachments = self.env["ir.attachment"].sudo().create({
                "name": self.media_filename or "whatsapp-media",
                "datas": self.media_data,
                "mimetype": self.media_mimetype or "application/octet-stream",
                "res_model": "discuss.channel",
                "res_id": channel.id,
            })
            if self.is_voice_note:
                # A WhatsApp voice note (ptt) plays inline in Discuss only if the
                # attachment carries discuss.voice.metadata — otherwise it renders
                # as a plain "download" file. This is the flag Odoo's own recorder
                # sets (see discuss_channel._create_attachments_for_post).
                attachments._set_voice_metadata()
        # In a group the author is the participant who wrote; in a private chat
        # it is the correspondent. The correspondent is the message *author*,
        # not a channel *member*, so operators get notified while the WhatsApp
        # contact never receives an Odoo email.
        author = self.partner_id or channel.whatsmeow_partner_id
        body = self.body or ""
        if not body and self.message_type != "text":
            body = self.env._("sent %s", self.message_type)
        # Inbound WhatsApp text is untrusted; `render_markup` escapes it before
        # turning WhatsApp's own markers into formatting, the same guarantee as
        # the chatter's Markup(...) % and the same rendering as its preview.
        # A WhatsApp reply quotes the message it answers; Discuss shows the same
        # thing as parent_id, so an inbound reply renders in the native reply
        # style instead of arriving as an unrelated bubble. Only a parent in
        # *this* channel qualifies — mail.message rejects a cross-thread parent,
        # and a quote of a message from before the conversation was routed has
        # no bubble at all.
        parent = self.reply_to_id.mail_message_id
        if parent.model != "discuss.channel" or parent.res_id != channel.id:
            parent = self.env["mail.message"]
        posted = channel.with_context(whatsmeow_skip_send=True).message_post(
            body=(Markup("<p>%s</p>") % render_markup(body)) if body else Markup(""),
            author_id=author.id or False,
            # Only reached when there is no author to name: `email_from` is
            # ignored by the client as soon as a persona exists, and a channel
            # notifies its members, never an address.
            email_from=None if author else self._wa_bubble_author_label(channel),
            message_type="comment",
            subtype_xmlid="mail.mt_comment",
            attachment_ids=attachments.ids,
            parent_id=parent.id or False,
        )
        self.mail_message_id = posted.id
