from odoo import fields, models


class WhatsmeowBotChatValue(models.Model):
    """One thing the bot collected, keyed by its step's store key.

    Rows rather than a JSON blob: they are searchable ("every chat where budget
    is over 10k"), groupable in a list view, and readable by a server action
    without anyone having to parse anything.
    """
    _name = "whatsmeow.bot.chat.value"
    _description = "Whatsmeow Chatbot Collected Value"
    _order = "id"

    chat_id = fields.Many2one(
        "whatsmeow.bot.chat", required=True, ondelete="cascade", index=True,
    )
    bot_id = fields.Many2one(related="chat_id.bot_id", store=True, index=True)
    partner_id = fields.Many2one(related="chat_id.partner_id", store=True, index=True)
    step_id = fields.Many2one("whatsmeow.bot.step", ondelete="set null")
    answer_id = fields.Many2one(
        "whatsmeow.bot.answer", string="Option Picked", ondelete="set null",
        help="Set when the value came from a numbered option rather than typed "
             "text, so a report can group on the option itself.",
    )
    key = fields.Char(required=True, index=True, help="Quote it as {{ answer.<key> }}.")
    label = fields.Char(help="What the step was called when this was collected.")
    value = fields.Char()

    _sql_constraints = [
        ("key_chat_uniq", "UNIQUE (chat_id, key)",
         "A chat stores one value per key — a second answer overwrites the first."),
    ]
