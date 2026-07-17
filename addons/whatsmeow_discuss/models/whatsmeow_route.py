from odoo import fields, models


class WhatsmeowRoute(models.Model):
    """One Discuss routing rule: when its criteria match a new conversation's
    first inbound message, its operators are added to the conversation's channel.

    Reuses the §9 match criteria via `whatsmeow.match.mixin`, so routing and
    inbound filtering match on exactly the same dimensions. First match wins,
    ordered by sequence — the same discipline as `whatsmeow.session.rule`.
    """
    _name = "whatsmeow.route"
    _inherit = "whatsmeow.match.mixin"
    _description = "Whatsmeow Discuss Routing Rule"
    _order = "session_id, sequence, id"

    session_id = fields.Many2one(
        "whatsmeow.session", required=True, ondelete="cascade", index=True,
    )
    sequence = fields.Integer(default=10)          # first match wins
    active = fields.Boolean(default=True)          # archive without deleting
    name = fields.Char()                           # optional human label
    user_ids = fields.Many2many(
        "res.users", string="Attend by",
        help="Operators added to the conversation's channel when this rule "
             "matches its first message. Leave empty to route a match to nobody "
             "(the channel is created, managers can still open it).",
    )
