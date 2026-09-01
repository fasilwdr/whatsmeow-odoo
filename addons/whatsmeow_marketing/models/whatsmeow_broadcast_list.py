from odoo import api, fields, models


class WhatsmeowBroadcastList(models.Model):
    """A named audience of broadcast contacts — the WhatsApp answer to a
    mailing list. Membership goes through `whatsmeow.broadcast.subscription`,
    so a contact can leave one list without leaving them all."""
    _name = "whatsmeow.broadcast.list"
    _description = "WhatsApp Broadcast List"
    _order = "name, id"

    name = fields.Char(required=True)
    active = fields.Boolean(default=True)
    user_id = fields.Many2one(
        "res.users", string="Owner", required=True, index=True,
        default=lambda self: self.env.user,
    )
    contact_ids = fields.Many2many(
        "whatsmeow.broadcast.contact", "whatsmeow_broadcast_subscription",
        "list_id", "contact_id", string="Contacts",
    )
    subscription_ids = fields.One2many(
        "whatsmeow.broadcast.subscription", "list_id", string="Subscriptions",
    )
    contact_count = fields.Integer(compute="_compute_counts")
    optout_count = fields.Integer(compute="_compute_counts", string="Opted Out")
    note = fields.Text(string="Notes")

    _name_owner_uniq = models.Constraint(
        "UNIQUE (name, user_id)",
        "You already have a broadcast list with that name.",
    )

    def _compute_counts(self):
        # One grouped read for the whole recordset rather than a search per
        # record — the same lesson whatsmeow_discuss._compute_channel_count
        # applies to its smart button.
        totals = dict(self.env["whatsmeow.broadcast.subscription"]._read_group(
            [("list_id", "in", self.ids)], groupby=["list_id"], aggregates=["__count"],
        ))
        opted = dict(self.env["whatsmeow.broadcast.subscription"]._read_group(
            [("list_id", "in", self.ids), ("contact_id.optout", "=", True)],
            groupby=["list_id"], aggregates=["__count"],
        ))
        for rec in self:
            rec.contact_count = totals.get(rec, 0)
            rec.optout_count = opted.get(rec, 0)

    def action_view_contacts(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": self.env._("Broadcast Contacts"),
            "res_model": "whatsmeow.broadcast.contact",
            "view_mode": "list,form",
            "domain": [("list_ids", "in", self.id)],
            "context": {"default_list_ids": [(4, self.id)]},
        }

    def _sendable_contacts(self):
        """Contacts of these lists a campaign may actually message.

        Opted-out contacts are dropped here as well as at trace resolution:
        this is what makes the count on the list honest before anyone presses
        send.
        """
        return self.env["whatsmeow.broadcast.contact"].search([
            ("list_ids", "in", self.ids),
            ("optout", "=", False),
            ("phone_tail", "!=", False),
        ])
