import logging
import math
from ast import literal_eval
from collections import defaultdict
from datetime import timedelta

from odoo import api, fields, models
from odoo.exceptions import UserError, ValidationError

from odoo.addons.whatsmeow.models.whatsmeow_message import REGISTRATION_TTL_DAYS

from .whatsmeow_broadcast_contact import phone_digits
from .whatsmeow_marketing_filter import SUPPORTED_MODELS

_logger = logging.getLogger(__name__)

# How many messages one campaign may put on the shared queue in a single pass.
# Not a rate limit — the session's own caps are that (PLAN.md §12.2) — but a
# bound on how far ahead of the queue this module ever runs. It is also the
# most this module will leave sitting `outgoing` for one session: pile up more
# and an operator's transactional message waits behind a blast, which is the
# whole reason campaigns are dripped rather than inserted in one go.
MARKETING_BATCH = 50


class WhatsmeowMarketingCampaign(models.Model):
    """One WhatsApp blast: a body, an audience, and a ledger of who got it.

    The module owns no transport. `action_send` creates no messages at all — it
    resolves the audience into traces and hands the campaign to a cron, which
    materialises `whatsmeow.message` rows a batch at a time into core's paced
    queue. Everything that makes a send survivable (pacing, the warm-up ramp,
    the daily and hourly caps, idempotency, the opt-out gate) is therefore
    inherited rather than re-implemented, and a mass send cannot outrun the
    limits a one-off send obeys.
    """
    _name = "whatsmeow.marketing.campaign"
    _description = "WhatsApp Marketing Campaign"
    _inherit = ["whatsmeow.render.mixin"]
    _order = "create_date desc, id desc"

    name = fields.Char(required=True, help="Internal name; recipients never see it.")
    user_id = fields.Many2one(
        "res.users", string="Owner", required=True, index=True,
        default=lambda self: self.env.user,
        help="Who is responsible for this campaign. A marketing user only ever "
             "sees their own.",
    )
    session_id = fields.Many2one(
        "whatsmeow.session", string="Send From", required=True, ondelete="restrict",
        index=True, help="The WhatsApp number this campaign sends from. Its "
                         "warm-up ramp and daily cap decide how fast the "
                         "campaign can drain.",
    )
    state = fields.Selection(
        [
            ("draft", "Draft"),
            ("sending", "Sending"),
            ("sent", "Sent"),
            ("cancelled", "Cancelled"),
        ],
        default="draft", required=True, copy=False, index=True,
        help="'Sent' means this campaign has finished handing its messages to "
             "WhatsApp — not that every recipient has received them. Delivered "
             "and Read keep arriving afterwards.",
    )
    sent_date = fields.Datetime(string="Finished On", readonly=True, copy=False)
    scheduled_date = fields.Datetime(
        string="Schedule",
        help="Leave empty to send as soon as you press Send. A date in the "
             "future starts the campaign automatically when it passes.",
    )

    body = fields.Text(
        string="Message",
        help="What the recipient reads. Placeholders are rendered per "
             "recipient: {{ object.name }}, {{ user.name }}, {{ company.name }}.",
    )
    body_is_static = fields.Boolean(compute="_compute_body_is_static")
    template_id = fields.Many2one(
        "whatsmeow.template", string="Load from Template", copy=False,
        help="Copies a saved template's body and attachments into this "
             "campaign. The campaign keeps its own copy afterwards, so editing "
             "the template later does not rewrite a campaign already sent.",
    )
    attachment_ids = fields.Many2many("ir.attachment", string="Attachment")

    # -- audience -------------------------------------------------------------
    recipient_source = fields.Selection(
        [
            ("list", "Broadcast Lists"),
            ("domain", "Contacts (filtered)"),
            ("filter", "Dynamic List"),
        ],
        default="list", required=True, string="Send To",
    )
    broadcast_list_ids = fields.Many2many(
        "whatsmeow.broadcast.list", string="Broadcast Lists",
    )
    mailing_model_id = fields.Many2one(
        "ir.model", string="Recipients Model",
        default=lambda self: self.env["ir.model"]._get("res.partner"),
        domain=[("model", "in", list(SUPPORTED_MODELS))],
        ondelete="cascade",
    )
    mailing_model_name = fields.Char(
        related="mailing_model_id.model", string="Model Name", store=True,
    )
    mailing_domain = fields.Char(string="Filter", default="[]")
    filter_id = fields.Many2one(
        "whatsmeow.marketing.filter", string="Dynamic List",
        help="A saved audience. Choosing one copies its model and filter onto "
             "this campaign, so a campaign already sent still shows who it was "
             "aimed at even if the dynamic list is edited later.",
    )
    recipient_preview_count = fields.Integer(
        compute="_compute_recipient_preview_count", string="Matching Recipients",
        help="How many records the audience matches right now. Resolved for "
             "real — and deduplicated — when the campaign is sent.",
    )

    # -- replies --------------------------------------------------------------
    reply_window_days = fields.Integer(
        string="Reply Window (days)", default=7,
        help="An inbound message from a recipient within this many days of "
             "their copy counts as a reply to this campaign. 0 means no limit, "
             "which will slowly credit unrelated conversations to it.",
    )
    route_to_discuss = fields.Boolean(
        string="Handle Replies in Discuss",
        help="Replies to this campaign open a Discuss conversation attended by "
             "the operators below, instead of following the session's own "
             "routing rules. The session itself must have 'Route inbound to "
             "Discuss' switched on.",
    )
    route_user_ids = fields.Many2many(
        "res.users", "whatsmeow_campaign_route_user_rel",
        "campaign_id", "user_id", string="Attended by",
    )

    # -- ledger ---------------------------------------------------------------
    trace_ids = fields.One2many(
        "whatsmeow.marketing.trace", "campaign_id", string="Traces",
    )
    total_count = fields.Integer(compute="_compute_stats", string="Recipients")
    pending_count = fields.Integer(compute="_compute_stats", string="Waiting")
    sent_count = fields.Integer(compute="_compute_stats", string="Sent")
    delivered_count = fields.Integer(compute="_compute_stats", string="Delivered")
    read_count = fields.Integer(compute="_compute_stats", string="Read")
    replied_count = fields.Integer(compute="_compute_stats", string="Replied")
    failed_count = fields.Integer(compute="_compute_stats", string="Failed")
    skipped_count = fields.Integer(compute="_compute_stats", string="Skipped")

    test_phone = fields.Char(
        string="Test Number",
        help="Send yourself one copy before the blast. It is rendered against "
             "the first recipient, creates no trace, and goes out immediately.",
    )

    # -- computes -------------------------------------------------------------
    @api.depends("recipient_source", "mailing_model_name")
    def _compute_render_model(self):
        """Which model the body's {{ object.… }} refers to. A broadcast list is
        always a list of broadcast contacts; the other two sources say so."""
        for rec in self:
            rec.render_model = (
                "whatsmeow.broadcast.contact" if rec.recipient_source == "list"
                else rec.mailing_model_name or "res.partner"
            )

    @api.depends("body")
    def _compute_body_is_static(self):
        for rec in self:
            rec.body_is_static = bool(rec.body) and rec._body_is_static()

    @api.depends("recipient_source", "broadcast_list_ids", "mailing_model_name",
                 "mailing_domain")
    def _compute_recipient_preview_count(self):
        for rec in self:
            try:
                rec.recipient_preview_count = len(rec._recipient_records())
            except Exception:  # noqa: BLE001 - a half-typed domain must not break the form
                rec.recipient_preview_count = 0

    @api.depends("trace_ids.state", "trace_ids.replied")
    def _compute_stats(self):
        Trace = self.env["whatsmeow.marketing.trace"]
        by_state = defaultdict(dict)
        for campaign, state, count in Trace._read_group(
                [("campaign_id", "in", self.ids)],
                groupby=["campaign_id", "state"], aggregates=["__count"]):
            by_state[campaign.id][state] = count
        replied = dict(Trace._read_group(
            [("campaign_id", "in", self.ids), ("replied", "=", True)],
            groupby=["campaign_id"], aggregates=["__count"]))
        for rec in self:
            counts = by_state.get(rec.id, {})
            rec.total_count = sum(counts.values())
            rec.pending_count = counts.get("pending", 0) + counts.get("queued", 0)
            # "Sent" is everything that left for WhatsApp; a delivered or read
            # message was sent too, so the ladder counts upwards rather than
            # partitioning — otherwise Sent would fall as receipts arrive.
            rec.read_count = counts.get("read", 0)
            rec.delivered_count = counts.get("delivered", 0) + rec.read_count
            rec.sent_count = counts.get("sent", 0) + rec.delivered_count
            rec.failed_count = counts.get("failed", 0)
            rec.skipped_count = counts.get("skipped", 0) + counts.get("cancelled", 0)
            rec.replied_count = replied.get(rec, 0)

    # -- validation -----------------------------------------------------------
    @api.constrains("body", "attachment_ids")
    def _check_content(self):
        for rec in self:
            if not (rec.body or "").strip() and not rec.attachment_ids:
                raise ValidationError(self.env._(
                    "A campaign needs a message or a file to send."))

    @api.constrains("attachment_ids")
    def _check_single_attachment(self):
        # A whatsmeow.message carries one file, and one trace is one message.
        # Splitting a campaign into several messages per recipient would double
        # the traffic from the number and make the ledger ambiguous, so the
        # limit is enforced rather than silently truncated.
        for rec in self:
            if len(rec.attachment_ids) > 1:
                raise ValidationError(self.env._(
                    "A campaign sends one message per recipient, so it can carry "
                    "one file. Send the rest as a follow-up campaign."))

    @api.constrains("reply_window_days")
    def _check_reply_window(self):
        for rec in self:
            if rec.reply_window_days < 0:
                raise ValidationError(self.env._(
                    "The reply window cannot be negative."))

    @api.constrains("recipient_source", "mailing_model_id")
    def _check_model(self):
        for rec in self.filtered(lambda c: c.recipient_source != "list"):
            if rec.mailing_model_name not in SUPPORTED_MODELS:
                raise ValidationError(self.env._(
                    "A campaign can only target %s for now.",
                    ", ".join(SUPPORTED_MODELS)))

    # -- onchanges ------------------------------------------------------------
    @api.onchange("filter_id")
    def _onchange_filter_id(self):
        """A dynamic list is a starting point, not a live link: copy its model
        and domain onto the campaign so what was sent stays legible even if the
        filter is edited afterwards."""
        for rec in self.filtered("filter_id"):
            rec.mailing_model_id = rec.filter_id.model_id
            rec.mailing_domain = rec.filter_id.mailing_domain

    @api.onchange("template_id")
    def _onchange_template_id(self):
        for rec in self.filtered("template_id"):
            rec.body = rec.template_id.body
            if rec.template_id.attachment_ids:
                rec.attachment_ids = [(6, 0, rec.template_id.attachment_ids[:1].ids)]

    # -- audience -------------------------------------------------------------
    def _mailing_domain(self):
        self.ensure_one()
        try:
            return literal_eval(self.mailing_domain or "[]")
        except (ValueError, SyntaxError) as exc:
            raise UserError(self.env._(
                "The recipient filter is not a valid domain: %s", exc)) from exc

    def _recipient_records(self):
        """Everything this campaign is aimed at, as a recordset.

        Read as the campaign's owner would see it? No — as sudo. Sending is a
        cron job, and a record rule that hides a contact from *whoever happens
        to be running the cron* must not silently shrink someone's audience.
        The UI scoping in §9 is about who may look, not about who gets messaged.
        """
        self.ensure_one()
        if self.recipient_source == "list":
            return self.broadcast_list_ids.sudo()._sendable_contacts()
        model = self.mailing_model_name
        if not model or model not in self.env:
            return self.env["whatsmeow.broadcast.contact"].browse()
        return self.env[model].sudo().search(self._mailing_domain())

    def _recipient_identity(self, record):
        """(partner, broadcast contact, digits) for one recipient record."""
        self.ensure_one()
        if record._name == "whatsmeow.broadcast.contact":
            return record.partner_id, record, record.phone_digits
        return record, self.env["whatsmeow.broadcast.contact"], phone_digits(record.phone)

    def _skip_reason(self, partner, contact):
        """Why this recipient must not be messaged, or False.

        Both opt-out flags count: core's absolute one (never message this
        person at all) and the marketing one ('/stop'). The registration answer
        counts too, but only while it is fresh — past REGISTRATION_TTL_DAYS the
        gateway asks WhatsApp again, so a stale 'no' must stop blocking here as
        well (core's rule, kept in step).
        """
        self.ensure_one()
        if contact and contact.optout:
            return "optout"
        if partner:
            partner = partner.sudo()
            if partner.whatsmeow_optout or partner.whatsmeow_marketing_optout:
                return "optout"
            if partner.whatsmeow_registered == "no" and partner.whatsmeow_registered_date \
                    and partner.whatsmeow_registered_date >= fields.Datetime.now() \
                    - timedelta(days=REGISTRATION_TTL_DAYS):
                return "not_registered"
        return False

    def _resolve_recipients(self):
        """Turn the audience into traces — one per recipient, sendable or not.

        Deduplication is by the last 10 digits, because the same person is
        routinely in a broadcast list *and* in the partner domain, and getting
        the same blast twice is precisely what gets a number reported. The
        loser is kept as a `duplicate` trace rather than dropped, so the
        operator can see why a recipient they expected shows no message.
        """
        self.ensure_one()
        Trace = self.env["whatsmeow.marketing.trace"].sudo()
        # Traces from an earlier run of this same campaign (it was cancelled and
        # relaunched). Those numbers are already accounted for; re-messaging
        # them would be the duplicate this method exists to prevent.
        already = set(Trace.search([
            ("campaign_id", "=", self.id), ("skip_reason", "=", False),
        ]).mapped("phone_tail"))
        seen = set()
        vals_list = []
        for record in self._recipient_records():
            partner, contact, digits = self._recipient_identity(record)
            tail = digits[-10:]
            vals = {
                "campaign_id": self.id,
                "partner_id": partner.id or False,
                "contact_id": contact.id or False,
                "phone": digits,
                "phone_tail": tail,
            }
            if not digits:
                vals_list.append({**vals, "skip_reason": "no_phone"})
                continue
            if tail in already:
                continue  # a previous run already has this number on the ledger
            reason = self._skip_reason(partner, contact)
            if reason:
                vals_list.append({**vals, "skip_reason": reason})
                continue
            if tail in seen:
                vals_list.append({**vals, "skip_reason": "duplicate"})
                continue
            seen.add(tail)
            vals_list.append(vals)
        return Trace.create(vals_list)

    # -- sending --------------------------------------------------------------
    def action_send(self):
        """Resolve the audience and start sending.

        Creates no `whatsmeow.message` beyond the first small batch: the rest
        is dripped by `cron_process_marketing`, so the shared outgoing queue
        never fills with a week of blast ahead of an operator's transactional
        message.
        """
        for rec in self:
            if rec.state not in ("draft",):
                raise UserError(rec.env._(
                    "Only a draft campaign can be sent. This one is %s.",
                    dict(rec._fields["state"].selection)[rec.state]))
            if not (rec.body or "").strip() and not rec.attachment_ids:
                raise UserError(rec.env._("There is nothing to send."))
            traces = rec._resolve_recipients()
            sendable = traces.filtered(lambda t: not t.skip_reason)
            if not sendable and not rec.trace_ids.filtered(lambda t: not t.skip_reason):
                raise UserError(rec.env._(
                    "None of the %s recipient(s) can be messaged: they have no "
                    "number, have opted out, or are not on WhatsApp.", len(traces)))
            rec.write({"state": "sending", "sent_date": False})
            # A first batch inline, so pressing Send visibly does something.
            # Bounded by the same headroom the cron uses, so this cannot be a
            # way to jump the session's caps.
            rec._materialise(rec._session_headroom(rec.session_id))
            rec._check_done()
        return True

    def action_cancel(self):
        """Stop a campaign mid-blast.

        Only what has not left is undone: a queued message is deleted (WhatsApp
        never saw it), everything already sent stays on the ledger exactly as
        it happened.
        """
        for rec in self:
            if rec.state not in ("draft", "sending"):
                raise UserError(rec.env._("This campaign is already finished."))
            stopped = rec.trace_ids.filtered(lambda t: t.state in ("pending", "queued"))
            stopped.mapped("message_id").filtered(
                lambda m: m.state == "outgoing").sudo().unlink()
            stopped.sudo().write({"skip_reason": "cancelled"})
            rec.write({"state": "cancelled"})
        return True

    def action_reset_draft(self):
        """Put a cancelled campaign back on the desk.

        Traces that never went out are cleared so the audience is re-evaluated
        against today's opt-outs; traces that were sent are kept, and their
        numbers are skipped by the next resolution — a relaunch must not
        message the same person twice.
        """
        for rec in self:
            if rec.state != "cancelled":
                raise UserError(rec.env._(
                    "Only a cancelled campaign can be reset to draft."))
            rec.trace_ids.filtered(
                lambda t: t.state in ("skipped", "cancelled")).sudo().unlink()
            rec.write({"state": "draft", "sent_date": False})
        return True

    def _render_bodies(self, traces):
        """{trace id: the text that recipient gets}.

        One render call for the batch, not one per recipient: `_render_body`
        takes a list of ids for exactly this reason. A trace whose record is of
        another model (a converted contact, say) falls back to the raw body
        rather than rendering against the wrong record.
        """
        self.ensure_one()
        model = self.render_model
        records = {}
        for trace in traces:
            record = trace._recipient_record()
            if record and record._name == model:
                records[trace.id] = record.id
        rendered = self._render_body(list(set(records.values()))) if records else {}
        return {
            trace.id: rendered.get(records.get(trace.id), self.body or "")
            for trace in traces
        }

    def _media_vals(self):
        """The file this campaign carries, as message values."""
        self.ensure_one()
        attachment = self.attachment_ids[:1]
        if not attachment:
            return {}
        mimetype = attachment.mimetype or "application/octet-stream"
        return {
            "message_type": self.env["whatsmeow.message"]._kind_for_mimetype(mimetype),
            "media_data": attachment.datas,
            "media_filename": attachment.name,
            "media_mimetype": mimetype,
            "media_state": "none",
        }

    def _materialise(self, limit=MARKETING_BATCH):
        """Create up to `limit` outgoing messages from this campaign's waiting
        traces. Returns how many were created."""
        self.ensure_one()
        if self.state != "sending" or limit <= 0:
            return 0
        Trace = self.env["whatsmeow.marketing.trace"].sudo()
        traces = Trace.search(
            [("campaign_id", "=", self.id), ("state", "=", "pending")],
            limit=limit, order="id",
        )
        if not traces:
            return 0

        # A '/stop' may have landed since the audience was resolved. Re-check
        # now: not creating the message at all is cleaner than creating one for
        # the send gate to reject, and leaves no error row to explain.
        stopped = self.env["whatsmeow.marketing.trace"]
        for trace in traces:
            if self._skip_reason(trace.partner_id, trace.contact_id):
                stopped |= trace
        if stopped:
            stopped.write({"skip_reason": "optout"})
            traces -= stopped
        if not traces:
            return 0

        bodies = self._render_bodies(traces)
        media = self._media_vals()
        Message = self.env["whatsmeow.message"].sudo()
        created = 0
        for trace in traces:
            vals = {
                "session_id": self.session_id.id,
                "direction": "out",
                "phone": trace.phone,
                "partner_id": trace.partner_id.id or False,
                "marketing_trace_id": trace.id,
                "body": bodies.get(trace.id) or "",
                **media,
            }
            try:
                # One bad recipient (a number a constraint rejects) must not
                # cost the batch: skip it, mark it, keep going.
                with self.env.cr.savepoint():
                    message = Message.create(vals)
                    trace.message_id = message.id
                created += 1
            except Exception as exc:  # noqa: BLE001
                _logger.warning("whatsmeow.marketing: campaign %s could not queue "
                                "trace %s: %s", self.id, trace.id, exc)
                trace.write({"skip_reason": "no_phone"})
        return created

    def _check_done(self):
        """A campaign is finished when nothing is waiting or queued — not when
        every receipt is in. Delivered and Read keep updating afterwards."""
        self.ensure_one()
        if self.state != "sending":
            return
        remaining = self.env["whatsmeow.marketing.trace"].sudo().search_count([
            ("campaign_id", "=", self.id), ("state", "in", ("pending", "queued")),
        ])
        if not remaining:
            self.write({"state": "sent", "sent_date": fields.Datetime.now()})

    # -- pacing ---------------------------------------------------------------
    @api.model
    def _session_headroom(self, session):
        """How many marketing messages this number may take on right now.

        Three separate bounds, and the smallest wins:

        * one batch, minus whatever this module has already left `outgoing` on
          this session — so a stalled gateway cannot make the queue grow without
          limit, and transactional traffic is never queued behind a week of blast;
        * the session's share of its own daily allowance (`marketing_daily_share`)
          — a blast must not eat the whole day and leave nothing for the order
          confirmations the number exists to send;
        * what is left of the daily cap itself, marketing or not.

        `_daily_cap() == 0` means the warm-up is off and the day is unlimited;
        the batch alone bounds the pass then.
        """
        Message = self.env["whatsmeow.message"].sudo()
        queued = Message.search_count([
            ("session_id", "=", session.id),
            ("state", "=", "outgoing"),
            ("marketing_trace_id", "!=", False),
        ])
        headroom = MARKETING_BATCH - queued
        cap = session._daily_cap()
        if cap:
            midnight = session._local_midnight_utc()
            share = min(100, max(0, session.marketing_daily_share))
            allowance = math.floor(cap * share / 100.0)
            marketing_today = Message.search_count([
                ("session_id", "=", session.id),
                ("marketing_trace_id", "!=", False),
                ("sent_date", ">=", midnight),
            ])
            headroom = min(headroom, allowance - marketing_today,
                           cap - session._sent_since(midnight))
        return max(0, headroom)

    # -- cron -----------------------------------------------------------------
    @api.model
    def cron_process_marketing(self):
        """Start what is due, then feed each number as much as it can take.

        Campaigns sharing a number take equal slices of that number's headroom,
        so a 5,000-recipient blast cannot indefinitely block a 40-recipient one
        behind it — the same fairness `cron_process_outgoing` gives sessions.
        """
        now = fields.Datetime.now()
        due = self.search([
            ("state", "=", "draft"),
            ("scheduled_date", "!=", False),
            ("scheduled_date", "<=", now),
        ])
        for campaign in due:
            try:
                with self.env.cr.savepoint():
                    campaign.action_send()
            except Exception as exc:  # noqa: BLE001 - one bad campaign must not stop the rest
                _logger.warning("whatsmeow.marketing: campaign %s could not start: %s",
                                campaign.id, exc)

        sending = self.search([("state", "=", "sending")], order="create_date asc")
        by_session = defaultdict(list)
        for campaign in sending:
            by_session[campaign.session_id].append(campaign)

        for session, campaigns in by_session.items():
            headroom = self._session_headroom(session)
            if headroom <= 0:
                continue
            slice_size = max(1, headroom // len(campaigns))
            for campaign in campaigns:
                if headroom <= 0:
                    break
                try:
                    headroom -= campaign._materialise(min(slice_size, headroom))
                    campaign._check_done()
                except Exception as exc:  # noqa: BLE001
                    _logger.warning("whatsmeow.marketing: campaign %s failed to "
                                    "queue a batch: %s", campaign.id, exc)
                    continue
                # Each batch made durable before the next: the rows this pass
                # created are minutes away from being sent, and re-rendering
                # them after a crash would double-message the recipients.
                self.env["whatsmeow.message"]._commit_progress()

    # -- test send ------------------------------------------------------------
    def action_send_test(self):
        """One copy to a number of your choosing, sent immediately.

        Rendered against the first recipient so the placeholders are exercised
        on real data — a blast nobody previewed on a handset is how a broken
        {{ }} reaches everyone at once. No trace: a test is not a recipient.
        """
        self.ensure_one()
        digits = phone_digits(self.test_phone)
        if not digits:
            raise UserError(self.env._("Enter a number to send the test to."))
        record = self._recipient_records()[:1]
        body = self.body or ""
        if record and record._name == self.render_model:
            body = self._render_body(record.ids).get(record.id, body)
        message = self.env["whatsmeow.message"].create({
            "session_id": self.session_id.id,
            "direction": "out",
            "phone": digits,
            "body": body,
            **self._media_vals(),
        })
        message.action_send()
        if message.state == "error":
            raise UserError(self.env._(
                "The test message could not be sent: %s", message.error_message))
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "message": self.env._("Test message sent to %s.", self.test_phone),
                "type": "success",
            },
        }

    # -- UI -------------------------------------------------------------------
    def action_view_traces(self, states=None):
        self.ensure_one()
        domain = [("campaign_id", "=", self.id)]
        if states:
            domain.append(("state", "in", states))
        return {
            "type": "ir.actions.act_window",
            "name": self.env._("Recipients"),
            "res_model": "whatsmeow.marketing.trace",
            "view_mode": "list,form",
            "domain": domain,
            "context": {"create": False},
        }

    def action_view_sent(self):
        return self.action_view_traces(["sent", "delivered", "read"])

    def action_view_delivered(self):
        return self.action_view_traces(["delivered", "read"])

    def action_view_read(self):
        return self.action_view_traces(["read"])

    def action_view_failed(self):
        return self.action_view_traces(["failed"])

    def action_view_skipped(self):
        return self.action_view_traces(["skipped", "cancelled"])

    def action_view_replied(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": self.env._("Replies"),
            "res_model": "whatsmeow.marketing.trace",
            "view_mode": "list,form",
            "domain": [("campaign_id", "=", self.id), ("replied", "=", True)],
            "context": {"create": False},
        }

    def action_open_recipients(self):
        """Look at the audience before sending to it. A campaign whose
        recipients you cannot inspect is how 4,000 people get the wrong
        message."""
        self.ensure_one()
        records = self._recipient_records()
        return {
            "type": "ir.actions.act_window",
            "name": self.env._("Recipients"),
            "res_model": records._name,
            "view_mode": "list,form",
            "domain": [("id", "in", records.ids)],
            "context": {"create": False},
        }
