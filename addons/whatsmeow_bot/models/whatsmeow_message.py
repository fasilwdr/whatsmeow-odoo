import logging

from odoo import fields, models

_logger = logging.getLogger(__name__)


class WhatsmeowMessage(models.Model):
    _inherit = "whatsmeow.message"

    bot_chat_id = fields.Many2one(
        "whatsmeow.bot.chat", string="Chatbot Chat", index=True, ondelete="set null",
        help="The chatbot conversation this message belongs to — the reply it "
             "answered, or the line the bot sent.",
    )

    def _deliver_inbound(self):
        """Give the chatbot first refusal on an accepted inbound message.

        `_deliver_inbound` is the seam core left for exactly this: the bot is
        another destination a message can land in, so it is another override
        rather than another branch inside the webhook. Falling through to
        `super()` reaches the Discuss bridge and then the chatter, unchanged.

        The bot runs inside a savepoint. A workflow is written by an admin in a
        designer, so it can be wrong in ways core code cannot — a server action
        that raises, a body that references a deleted step — and the one thing
        that must never happen is losing the contact's message because of it.
        On a failure the savepoint rolls the bot's work back and the message is
        delivered the ordinary way, as though no bot existed.
        """
        self.ensure_one()
        consumed = False
        try:
            with self.env.cr.savepoint():
                consumed = self._whatsmeow_bot_dispatch()
        except Exception:  # noqa: BLE001 - a broken flow must not lose a message
            _logger.exception(
                "whatsmeow_bot: chatbot dispatch failed for message %s; "
                "delivering it normally", self.id)
            consumed = False
        if consumed:
            return
        if self._whatsmeow_bot_deliver_to_handover():
            return
        return super()._deliver_inbound()

    def _whatsmeow_bot_deliver_to_handover(self):
        """Keep a handed-over conversation in the channel the handover opened.

        A handover opens the contact's Discuss conversation whether or not the
        session routes inbound there, because that is where the operator was
        sent. Without this the *next* message would land on the contact's
        chatter instead, and the colleague would be sitting in a thread that
        never updates again — a dead end with a person in it.

        Sessions that already route to Discuss need none of this, so the check
        starts by getting out of their way.
        """
        self.ensure_one()
        if self.session_id.route_to_discuss:
            return False
        chat = self.env["whatsmeow.bot.chat"].sudo().search([
            ("session_id", "=", self.session_id.id),
            ("chat_jid", "=", self.chat_jid or ""),
            ("state", "=", "handover"),
        ], order="id desc", limit=1)
        if not chat.channel_id:
            return False
        self.sudo()._wa_post_into_channel(chat.channel_id)
        return True

    def _whatsmeow_bot_dispatch(self):
        """Run the chatbot for this message. True when it owns the delivery.

        Owning it means the message does not also land in an operator's inbox:
        while a bot is asking someone for their order number, that exchange is
        the bot's, and a channel lighting up for every line of it is how a
        useful bot becomes a muted one. `deliver_to_inbox` turns that off for
        admins who want to watch, and a handover posts the message itself into
        the conversation, so nothing is ever silently dropped.
        """
        self.ensure_one()
        if self.direction != "in" or self.is_placeholder:
            # A placeholder is WhatsApp's empty first copy; judging a reply on
            # it would answer a question the contact has not actually answered.
            return False
        Chat = self.env["whatsmeow.bot.chat"].sudo()
        chat = Chat._find_running(self.session_id, self.chat_jid or "")
        if chat:
            self.sudo().bot_chat_id = chat.id
            chat._handle_inbound(self)
        else:
            bot = self.env["whatsmeow.bot"].sudo()._match(self)
            if not bot or bot._is_blocked(self):
                return False
            chat = Chat._open(bot, self)
            self.sudo().bot_chat_id = chat.id
            chat._handle_start(self)
        if chat.bot_id.deliver_to_inbox:
            # A handover already posted this message into the conversation;
            # letting `super()` post it again would double it.
            return bool(self.mail_message_id)
        return True
