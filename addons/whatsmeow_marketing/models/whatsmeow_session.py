import logging

from odoo import api, fields, models
from odoo.exceptions import ValidationError

from odoo.addons.whatsmeow.models.whatsmeow_match_mixin import _split

_logger = logging.getLogger(__name__)

DEFAULT_STOP = "/stop"
DEFAULT_START = "/start"


class WhatsmeowSession(models.Model):
    """Marketing's per-number settings, and its two hooks into the inbound path.

    Both hooks are core seams called from the webhook, so nothing in core knows
    this module exists: installing it is what changes behaviour.
    """
    _inherit = "whatsmeow.session"

    marketing_stop_keywords = fields.Char(
        string="Stop Keywords", default=DEFAULT_STOP,
        help="Comma-separated. A recipient whose whole message is one of these "
             "is opted out of marketing — campaigns skip them from then on. "
             "Matched on the entire message, not as a substring: \"please don't "
             "stop sending these\" must not unsubscribe an enthusiastic customer.",
    )
    marketing_start_keywords = fields.Char(
        string="Start Keywords", default=DEFAULT_START,
        help="Comma-separated. Undoes a stop, so a mistaken '/stop' is "
             "recoverable by the contact themselves.",
    )
    marketing_daily_share = fields.Integer(
        string="Marketing Share (%)", default=80,
        help="The most of this number's daily allowance campaigns may use. The "
             "rest is left for order confirmations and operator replies — the "
             "traffic the number exists for. 100 lets a campaign use the whole "
             "day's allowance.",
    )
    campaign_ids = fields.One2many(
        "whatsmeow.marketing.campaign", "session_id", string="Campaigns",
    )

    @api.constrains("marketing_daily_share")
    def _check_marketing_share(self):
        for rec in self:
            if not 0 <= rec.marketing_daily_share <= 100:
                raise ValidationError(self.env._(
                    "The marketing share is a percentage: use a value between "
                    "0 and 100."))

    # -- inbound keywords -----------------------------------------------------
    def _inbound_optout(self, facts, partner):
        """Honour '/stop' and '/start' alongside core's rule-based opt-out.

        This seam is the right place for both of core's reasons: it runs
        *before* the inbound filter's accept/reject, so a session that rejects
        unknown senders still honours a '/stop' from one — someone asking to be
        left alone is the last person whose message should be dropped unread —
        and it runs again from `_merge_duplicate` on the real copy, so
        WhatsApp's empty first delivery cannot swallow the keyword.
        """
        result = super()._inbound_optout(facts, partner)
        self._marketing_keyword(facts, partner)
        return result

    def _marketing_targets(self, facts, partner):
        """Everyone this sender is, as far as marketing is concerned.

        A person is routinely in two tables — a `res.partner` and a broadcast
        contact imported from a list — and stopping only one of them means the
        next campaign reaches them anyway.
        """
        self.ensure_one()
        contacts = self.env["whatsmeow.broadcast.contact"]
        tail = facts.get("phone_tail")
        if tail:
            contacts = contacts.sudo().search([("phone_tail", "=", tail)])
        if partner:
            contacts |= contacts.sudo().search([("partner_id", "=", partner.id)])
        return partner.sudo() if partner else self.env["res.partner"], contacts

    def _marketing_keyword(self, facts, partner):
        """Apply a marketing stop/start keyword, if the message is one.

        Matched on the *whole* trimmed message. A substring match would opt out
        anyone who wrote "don't stop", and an opt-out nobody asked for is worse
        than a missed one — it silently ends the relationship.
        """
        self.ensure_one()
        if facts.get("is_placeholder"):
            # WhatsApp's empty first copy says nothing about what was written.
            # The real copy comes back through here via `_merge_duplicate`.
            return False
        # The webhook lower-cases the body when it builds the facts.
        body = (facts.get("body") or "").strip()
        if not body:
            return False
        stops = {kw.lower() for kw in _split(self.marketing_stop_keywords)}
        starts = {kw.lower() for kw in _split(self.marketing_start_keywords)}
        if body in stops:
            action = "stop"
        elif body in starts:
            action = "start"
        else:
            return False

        target_partner, contacts = self._marketing_targets(facts, partner)
        if not target_partner and not contacts:
            # A LID-only sender has no partner and no number: there is nobody
            # to flag. Same limitation core documents for its own opt-out.
            _logger.info("whatsmeow.marketing: session %s saw '%s' from a sender "
                         "it cannot identify", self.code, body)
            return False

        if action == "stop":
            reason = self.env._("Replied '%s' on WhatsApp", body)
            target_partner._whatsmeow_marketing_optout(reason)
            contacts.sudo()._marketing_optout(reason)
        else:
            target_partner._whatsmeow_marketing_optin(
                self.env._("Replied '%s' on WhatsApp — marketing resumed", body))
            contacts.sudo()._marketing_optin()
        _logger.info("whatsmeow.marketing: session %s applied '%s' to partner %s "
                     "and %s broadcast contact(s)",
                     self.code, body, target_partner.id or "-", len(contacts))
        return True

    # -- Discuss routing ------------------------------------------------------
    def _route_users(self, facts):
        """Let a campaign claim its own replies.

        The session's routing rules are about who a *conversation* belongs to;
        a campaign is about who is answering for *this blast*, which is usually
        whoever ran it. When the reply cannot be attributed to a campaign, the
        session's own rules decide exactly as before.
        """
        self.ensure_one()
        trace = self.env["whatsmeow.marketing.trace"]._find_for_reply(
            self, self.env["res.partner"].browse(facts.get("partner_id") or []),
            facts.get("phone_tail"),
        )
        campaign = trace.campaign_id
        if campaign.route_to_discuss and campaign.route_user_ids:
            return campaign.route_user_ids
        return super()._route_users(facts)
