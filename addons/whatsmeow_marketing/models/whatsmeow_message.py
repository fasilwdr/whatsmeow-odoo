import logging

from odoo import fields, models

from odoo.addons.whatsmeow.models.whatsmeow_match_mixin import _phone_tail

_logger = logging.getLogger(__name__)


class WhatsmeowMessage(models.Model):
    """Two seams, both already in core, both used exactly as core intends.

    * `_optout_block_reason` — the last gate before a message leaves. A '/stop'
      landing after the campaign queued this row still stops it here.
    * `_deliver_inbound` — where an accepted incoming message is handed on. A
      reply to a campaign is credited on the way through.
    """
    _inherit = "whatsmeow.message"

    marketing_trace_id = fields.Many2one(
        "whatsmeow.marketing.trace", string="Campaign Recipient",
        ondelete="set null", index=True, copy=False,
        help="The campaign ledger row this message belongs to. Set only on "
             "messages a campaign queued.",
    )
    marketing_campaign_id = fields.Many2one(
        related="marketing_trace_id.campaign_id", string="Campaign",
        store=True, index=True,
    )

    def _optout_block_reason(self):
        """Add the marketing opt-out to core's absolute one.

        Only campaign messages are gated: '/stop' means "no more promotions",
        not "never message me again", so an operator's reply and a delivery
        notification must still get through (see §7.1). A client who wants
        '/stop' to mean everything adds a core `whatsmeow.session.rule` with
        `action='optout'` and gets exactly that, with no code.
        """
        self.ensure_one()
        reason = super()._optout_block_reason()
        if reason or not self.marketing_trace_id:
            return reason
        trace = self.marketing_trace_id.sudo()
        if trace.contact_id.optout:
            return self.env._(
                "%s has opted out of WhatsApp marketing.", trace.contact_id.name)
        partner = (trace.partner_id or self._optout_partner()).sudo()
        if partner and partner.whatsmeow_marketing_optout:
            return self.env._(
                "%s has opted out of WhatsApp marketing.", partner.display_name)
        return False

    def _deliver_inbound(self):
        """Credit a reply to the campaign it answers, then deliver as usual.

        The attribution runs *before* `super()`, deliberately: the Discuss
        bridge creates the conversation's channel inside that call and
        evaluates routing while doing it, and campaign routing (§8) needs what
        this writes. Attending the channel runs *after*, because for an
        existing conversation there is nothing to join until super() has found
        it.
        """
        self.ensure_one()
        trace = self._marketing_note_reply()
        result = super()._deliver_inbound()
        self._marketing_attend_channel(trace)
        return result

    def _marketing_attend_channel(self, trace):
        """Put the campaign's operators on an existing conversation.

        Routing only runs when a channel is *created* (core's rule, so a manual
        reassignment survives the next message). A campaign reply from someone
        who already has an open conversation would therefore never reach the
        people running the campaign — and that is most repeat customers. So
        they are added here, and only ever added: evicting whoever was already
        attending would lose an operator mid-conversation.
        """
        self.ensure_one()
        campaign = trace.campaign_id if trace else None
        if not campaign or not campaign.route_to_discuss or not campaign.route_user_ids:
            return
        bubble = self.mail_message_id
        if not bubble or bubble.model != "discuss.channel":
            return  # not a routed conversation (the session posts to the chatter)
        channel = self.env["discuss.channel"].sudo().browse(bubble.res_id).exists()
        if not channel:
            return
        channel._add_members(
            partners=campaign.route_user_ids.partner_id,
            # A "joined the channel" line per operator per campaign reply would
            # bury the conversation it is announcing.
            post_joined_message=False,
        )

    def _marketing_trace_for_reply(self):
        """The campaign trace this inbound message answers, if any."""
        self.ensure_one()
        if self.direction != "in":
            return self.env["whatsmeow.marketing.trace"]
        return self.env["whatsmeow.marketing.trace"]._find_for_reply(
            self.session_id, self.partner_id, _phone_tail(self.phone))

    def _marketing_note_reply(self):
        """Mark this sender's campaign copy as replied to — once."""
        self.ensure_one()
        trace = self._marketing_trace_for_reply()
        if trace and trace._note_reply(self):
            _logger.info("whatsmeow.marketing: campaign %s credited a reply from %s",
                         trace.campaign_id.id, trace.display_name)
        return trace
