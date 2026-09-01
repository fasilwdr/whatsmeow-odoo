import re

from odoo import api, fields, models
from odoo.exceptions import UserError

DIGITS = re.compile(r"\D")


def phone_digits(value):
    """Every digit of a phone-ish string — what actually gets sent."""
    return DIGITS.sub("", value or "")


def phone_tail(value):
    """Last 10 digits, the identity used everywhere in this repo to decide
    whether two numbers are the same person. Same reduction as
    `whatsmeow.message._find_partner` and `whatsmeow.match.mixin._phone_tail`,
    so a '+44 7700 900123' here and a '447700900123' on a partner match."""
    return phone_digits(value)[-10:]


class WhatsmeowBroadcastContact(models.Model):
    """A marketing-only contact: a name and a number, nothing else.

    Deliberately *not* a `res.partner`. An imported broadcast list is unproven
    data — numbers that may not be on WhatsApp, people who never asked for
    anything — and pouring it into the customer database is how a CRM becomes
    unusable. A contact who turns out to be real gets converted (§3.3), which
    links an existing partner rather than creating a second one.
    """
    _name = "whatsmeow.broadcast.contact"
    _description = "WhatsApp Broadcast Contact"
    _order = "name, id"

    name = fields.Char(required=True)
    phone = fields.Char(
        required=True,
        help="With country code. Stored as typed; matching is on the digits, so "
             "'+44 7700 900123' and '447700900123' are the same person.",
    )
    phone_digits = fields.Char(
        compute="_compute_phone", store=True, readonly=True, index=True,
        help="The number with every non-digit stripped — what is actually sent.",
    )
    phone_tail = fields.Char(
        compute="_compute_phone", store=True, readonly=True, index=True,
        help="Last 10 digits: the identity used to recognise this person in an "
             "inbound message, in another list, or as an existing contact.",
    )
    partner_id = fields.Many2one(
        "res.partner", string="Contact", ondelete="set null", copy=False,
        help="Set once this broadcast contact has been converted into a real "
             "Odoo contact. Campaigns then message the contact, so the same "
             "person is never reached twice.",
    )
    list_ids = fields.Many2many(
        "whatsmeow.broadcast.list", "whatsmeow_broadcast_subscription",
        "contact_id", "list_id", string="Broadcast Lists",
    )
    subscription_ids = fields.One2many(
        "whatsmeow.broadcast.subscription", "contact_id", string="Subscriptions",
    )
    user_id = fields.Many2one(
        "res.users", string="Owner", required=True, index=True,
        default=lambda self: self.env.user,
        help="Who this contact belongs to. A marketing user only ever sees "
             "their own contacts.",
    )
    active = fields.Boolean(default=True)
    note = fields.Text(string="Notes")

    optout = fields.Boolean(
        string="Opted Out", copy=False,
        help="This contact asked to stop receiving marketing (usually by "
             "replying '/stop'). No campaign will include them until they "
             "write '/start'.",
    )
    optout_date = fields.Datetime(string="Opted Out On", readonly=True, copy=False)
    optout_reason = fields.Char(string="Opt-Out Reason", readonly=True, copy=False)

    trace_ids = fields.One2many(
        "whatsmeow.marketing.trace", "contact_id", string="Campaign History",
    )

    # Per owner, not global: two marketing users each importing their own list
    # must not collide on a number, and neither can see the other's contacts to
    # understand the error. Sending the same number twice is prevented where it
    # actually matters — per campaign (see whatsmeow.marketing.trace).
    _owner_phone_uniq = models.UniqueIndex(
        "(user_id, phone_tail) WHERE active AND phone_tail IS NOT NULL",
    )

    @api.depends("phone")
    def _compute_phone(self):
        for rec in self:
            rec.phone_digits = phone_digits(rec.phone)
            rec.phone_tail = phone_tail(rec.phone)

    @api.depends("name", "phone")
    def _compute_display_name(self):
        for rec in self:
            rec.display_name = f"{rec.name} ({rec.phone})" if rec.phone else rec.name

    # -- opt-out --------------------------------------------------------------
    def _marketing_optout(self, reason):
        """Record a marketing opt-out, keeping the first one's date and reason.

        Idempotent, like core's `res.partner._whatsmeow_optout`: WhatsApp can
        deliver the same '/stop' twice, and the audit trail should show when
        the contact actually asked, not when the retry landed.
        """
        for rec in self:
            if rec.optout:
                continue
            rec.write({
                "optout": True,
                "optout_date": fields.Datetime.now(),
                "optout_reason": reason,
            })

    def _marketing_optin(self):
        """Undo an opt-out — the '/start' half. The date and reason go with it:
        keeping them would leave the record reading as though the person were
        still opted out."""
        self.filtered("optout").write({
            "optout": False, "optout_date": False, "optout_reason": False,
        })

    # -- conversion -----------------------------------------------------------
    def action_convert_to_partner(self):
        """Turn a broadcast contact into a real Odoo contact.

        Link before create: a broadcast list is usually imported from somewhere
        the client already has customers, so the common case is that the
        partner exists and a second one would just split their history.

        The broadcast contact is kept, never deleted — past campaign traces
        point at it, and removing it would silently rewrite what happened.
        """
        Message = self.env["whatsmeow.message"]
        for rec in self:
            if rec.partner_id:
                continue
            if not rec.phone_digits:
                raise UserError(self.env._(
                    "%s has no usable phone number to convert.", rec.name))
            partner = Message._find_partner(rec.phone_digits)
            if not partner:
                partner = self.env["res.partner"].create({
                    "name": rec.name,
                    "phone": rec.phone,
                })
            rec.partner_id = partner
            # The wish follows the person, not the record type.
            if rec.optout:
                partner._whatsmeow_marketing_optout(
                    rec.optout_reason or self.env._("Converted from a broadcast contact"))
        return True

    def action_open_partner(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "res_model": "res.partner",
            "res_id": self.partner_id.id,
            "view_mode": "form",
        }
