import logging

from markupsafe import Markup
from psycopg2 import IntegrityError

from odoo import _, fields, models

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
        one, and an empty emoji clears it. The reaction rows are written
        directly rather than through `mail.message._message_add_reaction`, which
        in Odoo 16 always reacts as the current user — so this never reaches the
        relay hooks and cannot be echoed back out to WhatsApp."""
        self.ensure_one()
        bubble = self.mail_message_id
        if not bubble:
            return  # not a routed conversation — no bubble to annotate
        channel = (self.env["mail.channel"].sudo().browse(bubble.res_id)
                   if bubble.model == "mail.channel"
                   else self.env["mail.channel"])
        if not channel:
            return
        # A private chat's reactor may be unknown (LID-only); fall back to the
        # conversation's correspondent. A group reactor with no partner can't be
        # attributed, so it is dropped.
        reactor = partner or channel.whatsmeow_partner_id
        if not reactor:
            return
        # Odoo 16's `_message_add_reaction` always reacts *as the current user*,
        # so it cannot attribute a reaction to the WhatsApp correspondent. The
        # rows are therefore written directly and the channel is notified by
        # hand — the same payload `mail.channel._message_add_reaction_after_hook`
        # puts on the bus.
        Reaction = self.env["mail.message.reaction"].sudo()
        current = Reaction.search([
            ("message_id", "=", bubble.id), ("partner_id", "=", reactor.id),
        ])
        contents = current.mapped("content")
        for reaction in current:
            if reaction.content != emoji:
                content = reaction.content
                reaction.unlink()
                self._wa_notify_reaction(channel, bubble, content, reactor, "remove")
        if emoji and emoji not in contents:
            Reaction.create({
                "message_id": bubble.id,
                "content": emoji,
                "partner_id": reactor.id,
            })
            self._wa_notify_reaction(channel, bubble, emoji, reactor, "add")

    def _wa_notify_reaction(self, channel, bubble, content, reactor, action):
        """Tell open Discuss clients that a reaction appeared or went away."""
        reactions = self.env["mail.message.reaction"].sudo().search([
            ("message_id", "=", bubble.id), ("content", "=", content),
        ])
        command = "insert" if action == "add" else "insert-and-unlink"
        self.env["bus.bus"]._sendone(channel, "mail.message/insert", {
            "id": bubble.id,
            "messageReactionGroups": [
                ("insert" if reactions else "insert-and-unlink", {
                    "content": content,
                    "count": len(reactions),
                    "guests": [],
                    "message": {"id": bubble.id},
                    "partners": [(command, {"id": reactor.id})],
                }),
            ],
        })

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

    def _wa_channel_name(self, partner):
        self.ensure_one()
        if self.chat_type == "group":
            return self.chat_name or self.chat_jid or _("WhatsApp Group")
        return (partner.name or self.push_name or self.phone or self.chat_jid
                or _("WhatsApp"))

    # -- find or create the conversation's channel ---------------------------
    def _wa_get_or_create_channel(self):
        self.ensure_one()
        session = self.session_id
        Channel = self.env["mail.channel"].sudo()
        domain = [
            ("channel_type", "=", "whatsmeow"),
            ("whatsmeow_session_id", "=", session.id),
            ("whatsmeow_chat_jid", "=", self.chat_jid or ""),
        ]
        channel = Channel.search(domain, limit=1)
        if channel:
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

        # mail.channel.create always adds the acting user (here the webhook's
        # sudo user) as a member. Clear membership, so "member == attending
        # operator" holds — including the empty case (a conversation nobody was
        # routed to).
        channel.channel_member_ids.unlink()
        if operator_pids:
            # Through `add_members` rather than a create command, because that
            # is what pushes `mail.channel/joined` (with the channel_info the
            # client needs) onto each operator's bus. Members written straight
            # into the create appear only after a reload — from the operator's
            # side that looks exactly like routing not working.
            # `post_joined_message=False`: nobody joined anything, a contact
            # wrote in, and a "joined the channel" line would just be noise.
            channel.add_members(partner_ids=operator_pids,
                                post_joined_message=False)
        return channel

    # -- post the inbound message as a bubble --------------------------------
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
                "res_model": "mail.channel",
                "res_id": channel.id,
            })
            # Odoo 16's Discuss has no inline voice-note player (the
            # `discuss.voice.metadata` flag arrived in 17), so a WhatsApp voice
            # note posts as an ordinary audio attachment. `is_voice_note` still
            # records what it was.
        # In a group the author is the participant who wrote; in a private chat
        # it is the correspondent. The correspondent is the message *author*,
        # not a channel *member*, so operators get notified while the WhatsApp
        # contact never receives an Odoo email.
        author = self.partner_id or channel.whatsmeow_partner_id
        body = self.body or ""
        if not body and self.message_type != "text":
            body = _("sent %s", self.message_type)
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
        if parent.model != "mail.channel" or parent.res_id != channel.id:
            parent = self.env["mail.message"]
        posted = channel.with_context(whatsmeow_skip_send=True).message_post(
            body=(Markup("<p>%s</p>") % render_markup(body)) if body else Markup(""),
            author_id=author.id or False,
            message_type="comment",
            subtype_xmlid="mail.mt_comment",
            attachment_ids=attachments.ids,
            parent_id=parent.id or False,
        )
        self.mail_message_id = posted.id
