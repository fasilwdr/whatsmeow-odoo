import re

from odoo import fields, models

from .whatsmeow_message import CHAT_TYPES, MESSAGE_TYPES

DIGITS = re.compile(r"\D")


def _phone_tail(value):
    """Last 10 digits of a phone-ish string, or '' — the same reduction
    `whatsmeow.message._find_partner` uses, so a rule matches a stored number
    however it was formatted."""
    digits = DIGITS.sub("", value or "")
    return digits[-10:]


def _split(value):
    """Split a comma/newline-separated Char into a clean list of tokens."""
    return [tok.strip() for tok in re.split(r"[,\n]", value or "") if tok.strip()]


class WhatsmeowSessionRule(models.Model):
    _name = "whatsmeow.session.rule"
    _description = "Whatsmeow Inbound Filter Rule"
    _order = "session_id, sequence, id"

    session_id = fields.Many2one(
        "whatsmeow.session", required=True, ondelete="cascade", index=True,
    )
    sequence = fields.Integer(default=10)          # first match wins
    active = fields.Boolean(default=True)          # archive without deleting
    name = fields.Char()                           # optional human label
    action = fields.Selection(
        [("accept", "Accept"), ("reject", "Reject")],
        default="reject", required=True,
    )

    # --- match criteria (all *set* criteria are ANDed; an empty one is a wildcard) ---
    chat_type = fields.Selection(
        CHAT_TYPES, string="Chat type",
        help="Blank matches any. Same set as a message's chat type, so a rule "
             "can also drop broadcast/status/channel traffic.",
    )
    message_type = fields.Selection(
        MESSAGE_TYPES, string="Message type",
        help="Blank matches any. Same set as a message's kind (text/image/...).",
    )
    partner_ids = fields.Many2many(
        "res.partner", string="Contacts",
        help="Matches when the resolved sender is one of these. A LID-only "
             "contact has no partner, so an allowlist built on this drops it — "
             "use Sender LIDs to admit such a contact.",
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
        already-prefetched rule fields — so the evaluator stays cheap and
        trivially unit-testable. An all-empty rule matches everything.
        """
        self.ensure_one()
        if self.chat_type and self.chat_type != facts.get("chat_type"):
            return False
        if self.message_type and self.message_type != facts.get("message_type"):
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
            # so an identity-only rule still drops a placeholder immediately.
            if facts.get("is_placeholder"):
                return False
            if self.keyword.strip().lower() not in (facts.get("body") or ""):
                return False
        return True
