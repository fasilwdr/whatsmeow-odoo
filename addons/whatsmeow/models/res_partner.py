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

    # Sending to numbers that are not on WhatsApp is the other clearest
    # bulk-sender fingerprint (PLAN.md §12.4), and it is entirely preventable:
    # the gateway can ask before we burn a send.
    whatsmeow_registered = fields.Selection(
        [("yes", "On WhatsApp"), ("no", "Not on WhatsApp")],
        string="WhatsApp Number", copy=False, readonly=True,
        help="Blank means nobody has checked. A number known not to be "
             "registered is skipped by the queue rather than sent into the "
             "void — a high proportion of sends that never arrive is what a "
             "bought list looks like.",
    )
    whatsmeow_registered_date = fields.Datetime(
        string="Number Checked On", readonly=True, copy=False,
    )

    def write(self, vals):
        """A new number is a new question.

        The old answer was about the old number: keeping it would either block
        a good send or licence a bad one. Done in `write` rather than an
        onchange because most number changes come from an import or a sync,
        where no onchange ever runs.
        """
        if "phone" in vals and "whatsmeow_registered" not in vals:
            stale = self.filtered(
                lambda p: p.whatsmeow_registered
                and (p.phone or "") != (vals.get("phone") or "")
            )
            if stale:
                super(ResPartner, stale).write({
                    "whatsmeow_registered": False,
                    "whatsmeow_registered_date": False,
                })
        return super().write(vals)

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
