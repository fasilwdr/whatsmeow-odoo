from odoo import _, api, fields, models


class WhatsmeowSession(models.Model):
    _inherit = "whatsmeow.session"

    bot_enabled = fields.Boolean(
        string="Answer with a chatbot",
        help="Let chatbots pick up incoming messages on this number. Off by "
             "default — installing the module changes nothing until a number is "
             "opted in, the same discipline as Discuss routing.",
    )
    bot_ids = fields.Many2many(
        "whatsmeow.bot", string="Chatbots",
        relation="whatsmeow_bot_session_rel", column1="session_id", column2="bot_id",
        help="Bots that may answer on this number. A bot with no numbers set "
             "answers on every opted-in session, so this is only needed to pin "
             "a bot to particular numbers.",
    )
    bot_count = fields.Integer(compute="_compute_bot_count")
    bot_chat_count = fields.Integer(compute="_compute_bot_chat_count")

    @api.depends("bot_ids")
    def _compute_bot_count(self):
        for rec in self:
            rec.bot_count = len(rec.bot_ids)

    def _compute_bot_chat_count(self):
        groups = self.env["whatsmeow.bot.chat"].sudo().read_group(
            [("session_id", "in", self.ids)], fields=["session_id"],
            groupby=["session_id"])
        counts = {g["session_id"][0]: g["session_id_count"]
                  for g in groups if g["session_id"]}
        for rec in self:
            rec.bot_chat_count = counts.get(rec.id, 0)

    def action_view_bot_chats(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": _("Chatbot Chats"),
            "res_model": "whatsmeow.bot.chat",
            "view_mode": "tree,form",
            "domain": [("session_id", "=", self.id)],
            "context": {"create": False},
        }
