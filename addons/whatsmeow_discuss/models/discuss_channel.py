import logging

from odoo import fields, models, tools

_logger = logging.getLogger(__name__)


class DiscussChannel(models.Model):
    _inherit = "discuss.channel"

    channel_type = fields.Selection(
        selection_add=[("whatsmeow", "WhatsApp")],
        ondelete={"whatsmeow": "cascade"},
    )
    whatsmeow_session_id = fields.Many2one(
        "whatsmeow.session", string="WhatsApp Session", index=True, ondelete="cascade",
    )
    whatsmeow_chat_jid = fields.Char(string="WhatsApp Chat JID", index=True)
    whatsmeow_partner_id = fields.Many2one(
        "res.partner", string="WhatsApp Contact",
        help="The WhatsApp correspondent — the author of inbound bubbles, and "
             "never a channel member, so they receive no Odoo notification.",
    )

    # A conversation is (session, chat_jid). The partial unique index makes
    # find-or-create race-safe, the same discipline as `_inbound_wa_id_uniq`:
    # two inbound webhooks racing yield exactly one channel.
    _wa_conversation_uniq = models.UniqueIndex(
        "(whatsmeow_session_id, whatsmeow_chat_jid) WHERE channel_type = 'whatsmeow'",
    )

    def _types_allowing_unfollow(self):
        # Operators may leave a conversation (reassignment); the channel unpins.
        return super()._types_allowing_unfollow() + ["whatsmeow"]

    def _types_allowing_seen_infos(self):
        # Behave like a private conversation: members see read markers.
        return super()._types_allowing_seen_infos() + ["whatsmeow"]

    def _notify_thread(self, message, msg_vals=False, **kwargs):
        """Relay an operator's reply out over WhatsApp.

        `_notify_thread` is the post-send seam every `message_post` passes
        through — the same one Odoo's own WhatsApp module hooks. We let the
        normal notification happen, then send WhatsApp for our own channels.
        """
        rdata = super()._notify_thread(message, msg_vals=msg_vals, **kwargs)
        for channel in self.filtered(lambda c: c.channel_type == "whatsmeow"):
            channel._whatsmeow_relay(message)
        return rdata

    def _whatsmeow_relay(self, message):
        """Send a channel message back over WhatsApp, when it is an operator's
        reply — a human `comment` authored by an internal user, not our own
        inbound post."""
        self.ensure_one()
        if self.env.context.get("whatsmeow_skip_send"):
            return  # our own inbound post; never echo it back to WhatsApp
        if message.message_type != "comment":
            return  # joins/leaves/notifications are not messages to send
        author = message.author_id
        if not author or author == self.whatsmeow_partner_id:
            return  # authored by the correspondent (an inbound bubble)
        if not any(not user.share for user in author.user_ids):
            return  # only an internal operator's message is an outgoing reply
        session = self.whatsmeow_session_id
        if not session or not self.whatsmeow_chat_jid:
            return

        Message = self.env["whatsmeow.message"].sudo()
        base_vals = {
            "session_id": session.id,
            "direction": "out",
            "chat_jid": self.whatsmeow_chat_jid,
            "partner_id": self.whatsmeow_partner_id.id or False,
            "mail_message_id": message.id,
        }
        # Replying to a specific bubble in Discuss (parent_id, since channels are
        # not flat threads) should quote that message on WhatsApp, not send a
        # loose message. Find the whatsmeow.message behind the parent bubble and
        # let `_quote_payload` carry the quote.
        if message.parent_id:
            quoted = Message.search(
                [("mail_message_id", "=", message.parent_id.id)], limit=1)
            if quoted:
                base_vals["reply_to_id"] = quoted.id
        outgoing = Message
        text = tools.html2plaintext(message.body or "").strip()
        if text:
            outgoing |= Message.create({
                **base_vals, "message_type": "text", "body": text,
            })
        for att in message.attachment_ids:
            outgoing |= Message.create({
                **base_vals,
                "message_type": Message._kind_for_mimetype(att.mimetype),
                "body": "",
                "media_data": att.datas,
                "media_filename": att.name,
                "media_mimetype": att.mimetype,
                "media_size": att.file_size,
            })
        for msg in outgoing:
            self._whatsmeow_send_now(msg, message)

    def _whatsmeow_send_now(self, msg, mail_message):
        """Send a reply immediately, like `action_send` — a live reply is a
        human act at human speed, so it does not wait for the paced queue. A
        gateway failure notes the channel and must not roll back the operator's
        own message; the idempotency key still guards the rollback-after-POST
        case (§4.2)."""
        self.ensure_one()
        try:
            with self.env.cr.savepoint():
                msg.action_send()
        except Exception as exc:  # noqa: BLE001 - a failed reply must not raise
            _logger.warning("whatsmeow: relay of message %s failed: %s", msg.id, exc)
            self._whatsmeow_note_failure(mail_message, str(exc))
            return
        if msg.state == "error":
            self._whatsmeow_note_failure(mail_message, msg.error_message or "")

    def _whatsmeow_note_failure(self, mail_message, error):
        """Post the send failure back into the channel as a plain note, so the
        operator sees their reply did not go out. A notification never relays
        (only comments do), so this cannot loop."""
        self.ensure_one()
        self.with_context(whatsmeow_skip_send=True).message_post(
            body=self.env._("⚠️ WhatsApp could not send this reply: %s", error),
            message_type="notification",
            subtype_xmlid="mail.mt_note",
        )
