import re

from odoo import fields, models

from .whatsmeow_message import CHAT_TYPES, MESSAGE_TYPES

DIGITS = re.compile(r"\D")


def _phone_tail(value):
    """Last 10 digits of a phone-ish string, or '' — the same reduction
    `whatsmeow.message._find_partner` uses, so a criterion matches a stored
    number however it was formatted."""
    digits = DIGITS.sub("", value or "")
    return digits[-10:]


def _split(value):
    """Split a comma/newline-separated Char into a clean list of tokens."""
    return [tok.strip() for tok in re.split(r"[,\n]", value or "") if tok.strip()]


class WhatsmeowMatchMixin(models.AbstractModel):
    """Shared match criteria for anything that classifies an inbound message.

    Factored out of `whatsmeow.session.rule` (§9 inbound filtering) so the
    Discuss routing rules (`whatsmeow.route`, §10) match on exactly the same
    dimensions — the "define once" discipline that already governs
    `CHAT_TYPES`/`MESSAGE_TYPES` and `SESSION_CODE_RE` <-> `sessionNameRe`.

    An abstract mixin's fields land in each concrete table under the same
    column names, so inheriting this changes nothing about how
    `whatsmeow_session_rule` is stored — additive, no data migration.
    """
    _name = "whatsmeow.match.mixin"
    _description = "Whatsmeow Match Criteria"

    # --- match criteria (all *set* criteria are ANDed; an empty one is a wildcard) ---
    chat_type = fields.Selection(
        CHAT_TYPES, string="Chat type",
        help="Blank matches any. Same set as a message's chat type, so a rule "
             "can also match broadcast/status/channel traffic.",
    )
    message_type = fields.Selection(
        MESSAGE_TYPES, string="Message type",
        help="Blank matches any. Same set as a message's kind (text/image/...).",
    )
    sender_state = fields.Selection(
        [("new", "New (unknown sender)"), ("existing", "Existing (known contact)")],
        string="Sender",
        help="Blank matches any. 'New' = the sender resolved to no partner; "
             "'existing' = a known res.partner. Lets a rule single out first-time "
             "contacts (route them to the front desk) or known ones.",
    )
    partner_ids = fields.Many2many(
        "res.partner", string="Contacts",
        help="Matches when the resolved sender is one of these. A LID-only "
             "contact has no partner, so a rule built on this cannot match it — "
             "use Sender LIDs to reach such a contact.",
    )
    chat_jids = fields.Char(
        string="Chat JIDs", help="Comma/newline-separated chat JIDs (usually groups).",
    )
    phones = fields.Char(help="Sender phone fragments, last-10-digit match.")
    sender_lids = fields.Char(
        string="Sender LIDs",
        help="Sender LIDs — the only handle on a LID-only contact.",
    )
    keyword = fields.Char(help="Case-insensitive substring the body must contain.")

    def _matches(self, facts):
        """True when every *set* criterion matches the message facts.

        Pure Python over a small dict — no ORM in the hot loop beyond the
        already-prefetched criterion fields — so the evaluator stays cheap and
        trivially unit-testable. An all-empty rule matches everything.
        """
        self.ensure_one()
        if self.chat_type and self.chat_type != facts.get("chat_type"):
            return False
        if self.message_type and self.message_type != facts.get("message_type"):
            return False
        if self.sender_state and self.sender_state != facts.get("sender_state"):
            return False
        if self.partner_ids and facts.get("partner_id") not in self.partner_ids.ids:
            return False
        if self.chat_jids and facts.get("chat_jid") not in _split(self.chat_jids):
            return False
        if self.phones:
            tail = facts.get("phone_tail")
            if not tail or tail not in {_phone_tail(p) for p in _split(self.phones)}:
                return False
        if self.sender_lids and facts.get("sender_lid") not in _split(self.sender_lids):
            return False
        if self.keyword:
            # A placeholder's body is a stand-in (WhatsApp's empty first copy),
            # so a keyword cannot be judged on it: don't match, and let the real
            # copy be judged when it lands. Identity criteria above already ran,
            # so an identity-only rule still applies to a placeholder immediately.
            if facts.get("is_placeholder"):
                return False
            if self.keyword.strip().lower() not in (facts.get("body") or ""):
                return False
        return True
