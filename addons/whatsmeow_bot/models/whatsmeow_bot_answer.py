from odoo import _, api, fields, models
from odoo.exceptions import ValidationError


class WhatsmeowBotAnswer(models.Model):
    """One numbered option of a *choice* step.

    Its position in the list is the number the contact types, so sequence is
    not decoration: reordering options in the designer renumbers the message
    the bot sends.
    """
    _name = "whatsmeow.bot.answer"
    _description = "Whatsmeow Chatbot Option"
    _order = "sequence, id"

    step_id = fields.Many2one(
        "whatsmeow.bot.step", required=True, ondelete="cascade", index=True,
    )
    bot_id = fields.Many2one(related="step_id.bot_id", store=True, index=True)
    sequence = fields.Integer(default=10)
    name = fields.Char(
        string="Option", required=True,
        help="Shown to the contact, numbered. They can answer with the number "
             "or with these words.",
    )
    keywords = fields.Char(
        help="Extra words that pick this option, comma-separated — 'yes, y, ok'. "
             "The option's own text and its number always work.",
    )
    value = fields.Char(
        string="Stored As",
        help="What is filed under the step's store key when this option is "
             "picked. Empty stores the option's text, which is usually what a "
             "later message wants to quote back.",
    )
    next_step_id = fields.Many2one(
        "whatsmeow.bot.step", string="Go To", ondelete="set null",
        help="Where this option takes the conversation. Empty follows the "
             "step's own continuation, so a menu whose branches all rejoin needs "
             "nothing set here.",
    )
    server_action_ids = fields.Many2many(
        "ir.actions.server", string="Run Actions",
        relation="whatsmeow_bot_answer_action_rel",
        column1="answer_id", column2="action_id",
        help="Fired when this option is picked, after the step's own actions.",
    )

    @api.constrains("next_step_id", "step_id")
    def _check_next_step_bot(self):
        for rec in self:
            if rec.next_step_id and rec.next_step_id.bot_id != rec.step_id.bot_id:
                raise ValidationError(_(
                    "An option can only lead to a step of the same chatbot."
                ))

    def _run_action(self, chat):
        self.ensure_one()
        for action in self.server_action_ids:
            chat._run_server_action(action, step=self.step_id, answer=self)
