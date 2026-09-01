from odoo import api, fields, models


class ResPartner(models.Model):
    _inherit = "res.partner"

    # A second, softer flag beside core's absolute `whatsmeow_optout`.
    # "Stop sending me your promotions" is not "never message me again":
    # blocking the delivery notification for an order they placed, or an
    # operator's reply to their own question, would be a worse service failure
    # than the marketing they asked to stop.
    whatsmeow_marketing_optout = fields.Boolean(
        string="WhatsApp Marketing Opt-Out", copy=False,
        help="No marketing campaign will include this contact. Transactional "
             "messages and replies are unaffected — use 'WhatsApp Opt-Out' for "
             "an absolute stop. Set by hand, or when the contact replies "
             "'/stop'; cleared when they reply '/start'.",
    )
    whatsmeow_marketing_optout_date = fields.Datetime(
        string="Marketing Opt-Out On", readonly=True, copy=False,
    )
    whatsmeow_marketing_optout_reason = fields.Char(
        string="Marketing Opt-Out Reason", readonly=True, copy=False,
    )
    broadcast_contact_ids = fields.One2many(
        "whatsmeow.broadcast.contact", "partner_id", string="Broadcast Contacts",
    )

    @api.onchange("whatsmeow_marketing_optout")
    def _onchange_whatsmeow_marketing_optout(self):
        for rec in self:
            if rec.whatsmeow_marketing_optout and not rec.whatsmeow_marketing_optout_date:
                rec.whatsmeow_marketing_optout_date = fields.Datetime.now()
                rec.whatsmeow_marketing_optout_reason = self.env._("Set by hand")

    def _whatsmeow_marketing_optout(self, reason):
        """Record a marketing opt-out, keeping the first one's date and reason.

        Idempotent for the same reason core's `_whatsmeow_optout` is: WhatsApp
        can deliver the same '/stop' twice, and the audit trail should show when
        the contact actually asked, not when the retry landed.
        """
        for rec in self:
            if rec.whatsmeow_marketing_optout:
                continue
            rec.write({
                "whatsmeow_marketing_optout": True,
                "whatsmeow_marketing_optout_date": fields.Datetime.now(),
                "whatsmeow_marketing_optout_reason": reason,
            })

    def _whatsmeow_marketing_optin(self, reason):
        """The '/start' half. Deliberately not silent: an opt-in that leaves no
        trace is indistinguishable from someone quietly clearing the flag, and
        this is the record that says a person asked to hear from us again."""
        resumed = self.filtered("whatsmeow_marketing_optout")
        if not resumed:
            return
        resumed.write({
            "whatsmeow_marketing_optout": False,
            "whatsmeow_marketing_optout_date": False,
            "whatsmeow_marketing_optout_reason": False,
        })
        for rec in resumed:
            rec.message_post(body=reason)
