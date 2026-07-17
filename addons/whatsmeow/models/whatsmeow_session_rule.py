from odoo import fields, models


class WhatsmeowSessionRule(models.Model):
    _name = "whatsmeow.session.rule"
    _inherit = "whatsmeow.match.mixin"
    _description = "Whatsmeow Inbound Filter Rule"
    _order = "session_id, sequence, id"

    session_id = fields.Many2one(
        "whatsmeow.session", required=True, ondelete="cascade", index=True,
    )
    sequence = fields.Integer(default=10)          # first match wins
    active = fields.Boolean(default=True)          # archive without deleting
    name = fields.Char()                           # optional human label
    action = fields.Selection(
        [("accept", "Accept"), ("reject", "Reject")],
        default="reject", required=True,
    )
