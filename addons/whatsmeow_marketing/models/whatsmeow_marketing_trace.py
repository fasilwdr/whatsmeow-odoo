from datetime import timedelta

from odoo import api, fields, models

# How a message's own state reads on the ledger. Derived, never written by
# hand: a delivery receipt landing an hour after the send is handled entirely
# by core's webhook, and the trace follows. One source of truth is why
# "Delivered" on a campaign can never disagree with the message log.
STATE_BY_MESSAGE_STATE = {
    "outgoing": "queued",
    "sent": "sent",
    "delivered": "delivered",
    "read": "read",
    "error": "failed",
}

# Why a recipient was resolved but never messaged. 'cancelled' is the odd one
# out — it is not a fault of the recipient — so it renders as its own state.
SKIP_REASONS = [
    ("no_phone", "No phone number"),
    ("optout", "Opted out"),
    ("not_registered", "Not on WhatsApp"),
    ("duplicate", "Duplicate number in this campaign"),
    ("cancelled", "Campaign cancelled"),
]


class WhatsmeowMarketingTrace(models.Model):
    """One recipient of one campaign: the ledger row.

    It exists even when nothing was sent — a recipient with no number, an
    opted-out contact, a duplicate — because a campaign that reached 812 of 900
    must be able to say which 88 and why. Silence is not an acceptable answer
    to "did my customer get it?".
    """
    _name = "whatsmeow.marketing.trace"
    _description = "WhatsApp Campaign Trace"
    _order = "campaign_id desc, id"
    _rec_name = "display_name"

    campaign_id = fields.Many2one(
        "whatsmeow.marketing.campaign", string="Campaign",
        required=True, ondelete="cascade", index=True,
    )
    partner_id = fields.Many2one("res.partner", string="Contact", index=True)
    contact_id = fields.Many2one(
        "whatsmeow.broadcast.contact", string="Broadcast Contact",
        ondelete="cascade", index=True,
    )
    phone = fields.Char(string="Number", readonly=True, help="Digits, as sent.")
    phone_tail = fields.Char(
        readonly=True, index=True,
        help="Last 10 digits — the identity used to dedupe recipients and to "
             "recognise this person's reply.",
    )

    message_id = fields.Many2one(
        "whatsmeow.message", string="Message", ondelete="set null", index=True,
        help="The outgoing message this recipient's copy became. Empty until "
             "the campaign's next batch reaches them.",
    )
    sent_date = fields.Datetime(
        related="message_id.sent_date", store=True, index=True,
        help="When the gateway accepted this copy. The reply window counts from here.",
    )
    state = fields.Selection(
        [
            ("pending", "Waiting"),
            ("queued", "Queued"),
            ("sent", "Sent"),
            ("delivered", "Delivered"),
            ("read", "Read"),
            ("failed", "Failed"),
            ("skipped", "Skipped"),
            ("cancelled", "Cancelled"),
        ],
        compute="_compute_state", store=True, index=True, default="pending",
    )
    skip_reason = fields.Selection(
        SKIP_REASONS, readonly=True, copy=False,
        help="Why this recipient was never messaged.",
    )
    failure_reason = fields.Char(
        compute="_compute_state", store=True, readonly=True,
        help="What the gateway said, for a failed send.",
    )

    replied = fields.Boolean(readonly=True, copy=False, index=True)
    replied_date = fields.Datetime(readonly=True, copy=False)
    reply_message_id = fields.Many2one(
        "whatsmeow.message", string="Reply", ondelete="set null", copy=False,
        help="The inbound message credited to this campaign. Only the first "
             "one counts — a five-message conversation is one reply.",
    )

    # One sendable trace per number per campaign. The same person can easily be
    # in a broadcast list *and* the partner domain, and receiving the same
    # blast twice is exactly the report-and-block behaviour PLAN.md §12.1 warns
    # about. A Python check cannot promise this under a re-run or a race; the
    # index can. Skipped rows are excluded on purpose — the 'duplicate' trace
    # exists precisely to record the collision.
    _campaign_phone_uniq = models.UniqueIndex(
        "(campaign_id, phone_tail) WHERE skip_reason IS NULL AND phone_tail IS NOT NULL",
    )

    @api.depends("skip_reason", "message_id", "message_id.state",
                 "message_id.error_message")
    def _compute_state(self):
        for rec in self:
            rec.failure_reason = False
            if rec.skip_reason == "cancelled":
                rec.state = "cancelled"
            elif rec.skip_reason:
                rec.state = "skipped"
            elif not rec.message_id:
                rec.state = "pending"
            else:
                rec.state = STATE_BY_MESSAGE_STATE.get(rec.message_id.state, "sent")
                if rec.state == "failed":
                    rec.failure_reason = rec.message_id.error_message

    @api.depends("partner_id", "contact_id", "phone")
    def _compute_display_name(self):
        for rec in self:
            recipient = rec.partner_id.display_name or rec.contact_id.name
            rec.display_name = recipient or rec.phone or self.env._("Recipient")

    def _recipient_record(self):
        """The record this copy is rendered against.

        The broadcast contact wins when both are set: a converted contact keeps
        its `partner_id`, and the campaign that picked it up from a list wrote
        both, but `{{ object.name }}` in that campaign means the list entry.
        """
        self.ensure_one()
        return self.contact_id or self.partner_id

    def action_open_message(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "res_model": "whatsmeow.message",
            "res_id": self.message_id.id,
            "view_mode": "form",
        }

    # -- reply attribution ----------------------------------------------------
    @api.model
    def _find_for_reply(self, session, partner, tail):
        """The campaign an inbound message from this sender answers, if any.

        Newest first, first match wins, and only within that campaign's own
        reply window: without a window a customer writing in eight months later
        would be credited to a campaign nobody remembers, and every campaign's
        reply rate would creep upward forever.

        Identity is the partner *or* the number, because a broadcast contact
        has no partner and a partner may have been messaged before anyone
        matched them to one. A LID-only sender has neither, and is simply not
        attributable — the same limitation core already documents.
        """
        identity = []
        if partner:
            identity.append(("partner_id", "=", partner.id))
        if tail:
            identity.append(("phone_tail", "=", tail))
        if not identity:
            return self.browse()
        domain = [
            ("campaign_id.session_id", "=", session.id),
            ("sent_date", "!=", False),
        ] + ["|"] * (len(identity) - 1) + identity
        now = fields.Datetime.now()
        # A handful is plenty: they are ordered newest first, and a sender with
        # more than a few recent campaigns is answering the latest one.
        for trace in self.sudo().search(domain, order="sent_date desc", limit=10):
            window = trace.campaign_id.reply_window_days
            if not window or trace.sent_date >= now - timedelta(days=window):
                return trace
        return self.browse()

    def _note_reply(self, message):
        """Credit an inbound message to this trace, once."""
        self.ensure_one()
        if self.replied:
            return False
        self.sudo().write({
            "replied": True,
            "replied_date": fields.Datetime.now(),
            "reply_message_id": message.id,
        })
        return True
