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
        [("accept", "Accept"), ("reject", "Reject"), ("optout", "Opt the sender out")],
        default="reject", required=True,
        help="Accept/Reject decide whether the message is stored — first match "
             "wins. 'Opt the sender out' is not a disposition: it flags the "
             "contact so nothing is ever sent to them again, then lets the "
             "later rules decide what to do with this message (usually accept "
             "it, so the request itself is on file).",
    )
