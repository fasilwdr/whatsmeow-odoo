from odoo import fields, models


class WhatsmeowBotChatLine(models.Model):
    """One line of a chat's transcript.

    A copy, not a link, on purpose: it keeps reading correctly when the
    underlying `whatsmeow.message` is archived away by a retention policy, and
    it is what makes the chat form answer "what did the bot actually say" in
    one glance instead of three joins.
    """
    _name = "whatsmeow.bot.chat.line"
    _description = "Whatsmeow Chatbot Transcript Line"
    _order = "id"

    chat_id = fields.Many2one(
        "whatsmeow.bot.chat", required=True, ondelete="cascade", index=True,
    )
    date = fields.Datetime(default=fields.Datetime.now, readonly=True)
    direction = fields.Selection(
        [
            ("in", "Contact"),
            ("out", "Bot"),
            ("note", "System"),
        ],
        required=True, default="note",
        help="'System' lines are the bot's own bookkeeping — a server action "
             "that ran, a handover, a restart — and were never sent to anyone.",
    )
    body = fields.Text()
    step_id = fields.Many2one(
        "whatsmeow.bot.step", string="Step", ondelete="set null",
        help="Which step of the workflow this line belongs to.",
    )
    message_id = fields.Many2one(
        "whatsmeow.message", string="WhatsApp Message", ondelete="set null",
        help="The message log entry, when this line went over the wire.",
    )
    message_state = fields.Selection(related="message_id.state", string="Delivery")
