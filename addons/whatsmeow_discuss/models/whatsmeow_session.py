from odoo import api, fields, models


class WhatsmeowSession(models.Model):
    _inherit = "whatsmeow.session"

    route_to_discuss = fields.Boolean(
        string="Route inbound to Discuss",
        help="When on, an accepted inbound message opens (or continues) a Discuss "
             "conversation instead of posting to the contact's chatter. Off by "
             "default — an existing session behaves exactly as before.",
    )
    route_ids = fields.One2many(
        "whatsmeow.route", "session_id", string="Routing Rules",
    )
    route_count = fields.Integer(compute="_compute_route_count")
    route_fallback_user_ids = fields.Many2many(
        "res.users", string="Default operators",
        relation="whatsmeow_session_fallback_user_rel",
        help="Operators for a conversation that no routing rule matches. Empty = "
             "the channel is created with no operators (managers can still open it).",
    )
    auto_create_partner = fields.Boolean(
        string="Create contact for unknown senders", default=True,
        help="A Discuss conversation needs a correspondent. When an inbound sender "
             "is unknown, create a res.partner from their WhatsApp name / number. "
             "A LID-only sender has no phone and cannot be created, so its "
             "conversation falls back to a generic correspondent.",
    )
    channel_count = fields.Integer(compute="_compute_channel_count")

    @api.depends("route_ids")
    def _compute_route_count(self):
        for rec in self:
            rec.route_count = len(rec.route_ids)

    def _compute_channel_count(self):
        # Non-stored, no depends: recomputed on read, which is all a smart
        # button needs. A grouped count avoids one query per session.
        counts = dict(self.env["discuss.channel"]._read_group(
            [("channel_type", "=", "whatsmeow"),
             ("whatsmeow_session_id", "in", self.ids)],
            groupby=["whatsmeow_session_id"], aggregates=["__count"],
        ))
        for rec in self:
            rec.channel_count = counts.get(rec, 0)

    def _route_users(self, facts):
        """The operators who should attend a conversation opened by this message.

        First matching route wins (sequence, then id); if none match, the
        session's fallback operators. Archived routes are already excluded from
        `route_ids` by active_test. Pure Python over a prefetched One2many, and
        evaluated only once per conversation (on channel create).
        """
        self.ensure_one()
        for route in self.route_ids.sorted(key=lambda r: (r.sequence, r.id)):
            if route._matches(facts):
                return route.user_ids
        return self.route_fallback_user_ids

    def action_view_channels(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": self.env._("WhatsApp Conversations"),
            "res_model": "discuss.channel",
            "view_mode": "list,form",
            "domain": [("channel_type", "=", "whatsmeow"),
                       ("whatsmeow_session_id", "=", self.id)],
            "context": {"create": False},
        }
