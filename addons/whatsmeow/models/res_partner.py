from odoo import api, fields, models


class ResPartner(models.Model):
    _inherit = "res.partner"

    # Recipient blocks and reports are the dominant ban signal (PLAN.md §12.1),
    # so the cheapest large win is simply never messaging someone who asked us
    # to stop. The flag lives on the contact, not on a session: a person who
    # opted out did so from the company, not from one number.
    whatsmeow_optout = fields.Boolean(
        string="WhatsApp Opt-Out", copy=False,
        help="No WhatsApp message can be sent to this contact — not from the "
             "queue, the composer, a server action, or a Discuss reply. Set by "
             "hand, or automatically when the contact writes a keyword an "
             "opt-out rule matches.",
    )
    whatsmeow_optout_date = fields.Datetime(
        string="Opted Out On", readonly=True, copy=False,
    )
    whatsmeow_optout_reason = fields.Char(
        string="Opt-Out Reason", readonly=True, copy=False,
        help="How the opt-out came about — the inbound message that triggered "
             "it, or a note from whoever set it.",
    )

    @api.onchange("whatsmeow_optout")
    def _onchange_whatsmeow_optout(self):
        for rec in self:
            if rec.whatsmeow_optout and not rec.whatsmeow_optout_date:
                rec.whatsmeow_optout_date = fields.Datetime.now()
                rec.whatsmeow_optout_reason = self.env._("Set by hand")

    def _whatsmeow_optout(self, reason):
        """Record an opt-out, keeping the first one's date and reason.

        Called from the inbound path as sudo, and idempotent: WhatsApp can
        deliver the same "STOP" twice, and the audit trail should show when the
        contact actually asked, not when the retry landed.
        """
        for rec in self:
            if rec.whatsmeow_optout:
                continue
            rec.write({
                "whatsmeow_optout": True,
                "whatsmeow_optout_date": fields.Datetime.now(),
                "whatsmeow_optout_reason": reason,
            })
