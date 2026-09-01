from odoo import api, fields, models


class WhatsmeowBroadcastSubscription(models.Model):
    """`contact ↔ list`, carrying a per-list opt-out.

    A plain many2many could not hold the difference between "stop sending me
    the Ramadan offers" and "never message me again", and those are different
    wishes. `mailing.subscription`'s shape, for `mailing.subscription`'s reason.

    The per-list flag is stored and shown from day one but only *enforced* from
    phase 2 (see PLAN_WHATSAPP_MARKETING.md §3.2), so the UI never promises
    something the sender does not yet do.
    """
    _name = "whatsmeow.broadcast.subscription"
    _description = "WhatsApp Broadcast Subscription"
    _table = "whatsmeow_broadcast_subscription"
    _rec_name = "contact_id"
    _order = "list_id desc, contact_id desc"

    contact_id = fields.Many2one(
        "whatsmeow.broadcast.contact", string="Contact",
        required=True, ondelete="cascade", index=True,
    )
    list_id = fields.Many2one(
        "whatsmeow.broadcast.list", string="Broadcast List",
        required=True, ondelete="cascade", index=True,
    )
    opt_out = fields.Boolean(
        string="Opted Out of This List", default=False,
        help="The contact no longer wants this list in particular. A '/stop' "
             "reply opts them out of marketing entirely instead — see the "
             "contact's own Opted Out flag.",
    )
    opt_out_datetime = fields.Datetime(
        string="Unsubscription Date", compute="_compute_opt_out_datetime",
        store=True, readonly=False,
    )
    opt_out_reason = fields.Char(string="Reason")

    _contact_list_uniq = models.Constraint(
        "UNIQUE (contact_id, list_id)",
        "A contact cannot be subscribed to the same broadcast list twice.",
    )

    @api.depends("opt_out")
    def _compute_opt_out_datetime(self):
        self.filtered(lambda sub: not sub.opt_out).opt_out_datetime = False
        for sub in self.filtered("opt_out"):
            sub.opt_out_datetime = self.env.cr.now()
