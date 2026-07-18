import base64
import io
import logging
import math
import random
import re
from datetime import timedelta

import pytz
import qrcode

from odoo import _, api, fields, models
from odoo.addons.base.models.res_partner import _tz_get
from odoo.exceptions import ValidationError

_logger = logging.getLogger(__name__)

# Must stay in sync with sessionNameRe in gateway/main.go.
SESSION_CODE_RE = re.compile(r"^[a-z0-9_-]{1,40}$")

DIGITS = re.compile(r"\D")
# Must not exceed the gateway's WMG_CHECK_MAX_BATCH (default 50), which rejects
# an oversized batch outright.
CHECK_BATCH_SIZE = 50


class WhatsmeowSession(models.Model):
    _name = "whatsmeow.session"
    _description = "Whatsmeow WhatsApp Session"
    _order = "name"

    name = fields.Char(required=True)
    code = fields.Char(
        string="Session Key", required=True, copy=False,
        help="Lowercase letters, digits, '-' and '_' only. Used in gateway URLs.",
    )
    connection_id = fields.Many2one(
        "whatsmeow.connection", string="Gateway", required=True, ondelete="restrict",
        index=True,
    )
    status = fields.Selection(
        [
            ("draft", "Draft"),
            ("starting", "Starting"),
            ("qr", "Waiting for QR Scan"),
            ("connected", "Connected"),
            ("disconnected", "Disconnected"),
            ("logged_out", "Logged Out"),
            ("error", "Error"),
        ],
        default="draft", readonly=True, required=True,
    )
    jid = fields.Char(string="WhatsApp JID", readonly=True)
    qr_image = fields.Binary(string="Pairing QR", readonly=True, attachment=False)
    last_error = fields.Char(readonly=True)
    message_ids = fields.One2many("whatsmeow.message", "session_id")

    send_delay_min = fields.Integer(
        string="Min Delay (s)", default=3, required=True,
        help="Shortest pause the queue leaves between two sends from this number.",
    )
    send_delay_max = fields.Integer(
        string="Max Delay (s)", default=10, required=True,
        help="Longest pause between two sends. The queue waits a random time "
             "between the two bounds, so the traffic does not look metronomic.",
    )
    next_send_at = fields.Datetime(
        string="Next Send Allowed", readonly=True, copy=False,
        help="Set by the queue after each send. Until this moment passes, the "
             "queue leaves this number alone.",
    )

    # -- warm-up ramp and volume caps (PLAN.md §12.2) -------------------------
    # Pacing spaces sends apart but does not bound the day, and a young number
    # doing hundreds of sends on day one is a bulk-sender signature all by
    # itself. The cap is a curve rather than a schedule, so there is one knob
    # per client instead of a table to maintain.
    tz = fields.Selection(
        _tz_get, string="Timezone", default=lambda self: self.env.user.tz or "UTC",
        help="Whose midnight the daily allowance rolls over at. A cap that "
             "resets at UTC noon is wrong twice a day for a Gulf client.",
    )
    warmup_enabled = fields.Boolean(
        string="Warm-Up & Daily Cap", default=True,
        help="Bound how many queued messages this number sends per day, rising "
             "as the number ages. Off means unlimited — the pacing between "
             "sends still applies.",
    )
    warmup_start_date = fields.Date(
        string="Sending Since", copy=False,
        help="Day one of the ramp. Set automatically on the first send; move it "
             "back by hand if the number was already in use before this install.",
    )
    daily_cap_base = fields.Integer(
        string="Day-1 Allowance", default=20, required=True,
    )
    daily_cap_growth = fields.Float(
        string="Daily Growth", default=1.3, required=True,
        help="Multiplier per full day since the first send. 1.3 takes 20/day to "
             "the ceiling in about three weeks; 1.0 keeps the allowance flat.",
    )
    daily_cap_max = fields.Integer(
        string="Ceiling", default=500, required=True,
        help="The allowance stops growing here, however old the number is.",
    )
    hourly_cap = fields.Integer(
        string="Hourly Cap", default=0,
        help="0 derives it as a twelfth of the daily allowance, so a day's "
             "worth cannot go out in one hour.",
    )
    daily_cap = fields.Integer(
        compute="_compute_volume", string="Today's Allowance",
        help="0 means unlimited.",
    )
    sent_today = fields.Integer(compute="_compute_volume", string="Sent Today")
    sent_this_hour = fields.Integer(compute="_compute_volume", string="Sent This Hour")

    # -- inbound filtering ----------------------------------------------------
    inbound_default = fields.Selection(
        [("accept", "Accept"), ("reject", "Reject")],
        default="accept", required=True,
        string="Unmatched inbound messages",
        help="What to do with an incoming message that no rule matches. "
             "'Accept' + reject-rules = a blocklist; 'Reject' + accept-rules = "
             "an allowlist. The default 'Accept' keeps the session accepting "
             "everything, exactly as before any rule is added.",
    )
    inbound_rule_ids = fields.One2many(
        "whatsmeow.session.rule", "session_id", string="Inbound Filter Rules",
    )
    inbound_rule_count = fields.Integer(compute="_compute_inbound_rule_count")

    auto_mark_read = fields.Boolean(
        string="Auto Mark as Read", default=False,
        help="Send WhatsApp's read receipt (the blue ticks) as soon as an "
             "incoming message is accepted by the filter above. The sender is "
             "told the message reached you, not that anyone has read it — and "
             "the conversation stops showing as unread on the phone, so leave "
             "this off if the number is also watched from a handset.",
    )

    _code_conn_uniq = models.Constraint(
        "UNIQUE (code, connection_id)",
        "Session key must be unique per gateway connection.",
    )

    @api.constrains("code")
    def _check_code(self):
        for rec in self:
            if not SESSION_CODE_RE.match(rec.code or ""):
                raise ValidationError(_(
                    "Session key '%s' is invalid: use 1-40 characters of a-z, "
                    "0-9, '-' or '_'. The gateway rejects anything else.",
                    rec.code,
                ))

    @api.constrains("daily_cap_base", "daily_cap_growth", "daily_cap_max", "hourly_cap")
    def _check_caps(self):
        for rec in self:
            if min(rec.daily_cap_base, rec.daily_cap_max, rec.hourly_cap) < 0:
                raise ValidationError(_("Volume caps cannot be negative."))
            if rec.daily_cap_growth < 1.0:
                raise ValidationError(_(
                    "Daily growth cannot shrink the allowance: use 1.0 for a "
                    "flat cap, or more to ramp up."
                ))

    @api.constrains("send_delay_min", "send_delay_max")
    def _check_send_delays(self):
        for rec in self:
            if rec.send_delay_min < 0 or rec.send_delay_max < 0:
                raise ValidationError(_("Send delays cannot be negative."))
            if rec.send_delay_min > rec.send_delay_max:
                raise ValidationError(_(
                    "The minimum send delay (%(min)s s) cannot exceed the maximum "
                    "(%(max)s s).",
                    min=rec.send_delay_min, max=rec.send_delay_max,
                ))

    # -- inbound filtering ----------------------------------------------------
    @api.depends("inbound_rule_ids")
    def _compute_inbound_rule_count(self):
        for rec in self:
            rec.inbound_rule_count = len(rec.inbound_rule_ids)

    def _inbound_decision(self, facts):
        """Decide whether to accept an inbound message, given its facts dict.

        First matching rule wins (ordered by sequence, then id); if none match,
        fall back to `inbound_default`. Archived rules are already excluded from
        `inbound_rule_ids` by active_test. The rules are an already-prefetched
        One2many, so this is O(rules) pure Python per message.
        """
        self.ensure_one()
        for rule in self._sorted_inbound_rules():
            # An opt-out rule flags the sender (see `_inbound_optout`) but says
            # nothing about whether to keep the message, so it never wins the
            # disposition — the rules after it still get their say.
            if rule.action != "optout" and rule._matches(facts):
                return rule.action
        return self.inbound_default

    def _sorted_inbound_rules(self):
        self.ensure_one()
        return self.inbound_rule_ids.sorted(key=lambda r: (r.sequence, r.id))

    def _inbound_optout(self, facts, partner):
        """Honour an opt-out keyword in an incoming message.

        Kept as a rule action rather than a hard-coded "STOP"/"UNSUBSCRIBE"
        list so the wording is per-client data — the §9 matcher already does
        keyword matching, so this adds no matching code at all. A LID-only
        sender resolves to no partner and so cannot be flagged; the flag lives
        on the contact, and there is no contact to put it on.
        """
        self.ensure_one()
        if not partner:
            return False
        for rule in self._sorted_inbound_rules():
            if rule.action == "optout" and rule._matches(facts):
                partner.sudo()._whatsmeow_optout(self.env._(
                    "Asked to stop over WhatsApp (%s)", rule.display_name or self.name))
                _logger.info("whatsmeow: session %s opted partner %s out by rule %s",
                             self.code, partner.id, rule.id)
                return True
        return False

    def action_view_inbound_rules(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": self.env._("Inbound Filter Rules"),
            "res_model": "whatsmeow.session.rule",
            "view_mode": "list,form",
            "domain": [("session_id", "=", self.id)],
            "context": {"default_session_id": self.id},
        }

    # -- warm-up ramp and volume caps -----------------------------------------
    def _tz(self):
        self.ensure_one()
        try:
            return pytz.timezone(self.tz or "UTC")
        except pytz.UnknownTimeZoneError:  # pragma: no cover - defensive
            return pytz.UTC

    def _local_today(self):
        """Today's date where this number lives."""
        self.ensure_one()
        return pytz.UTC.localize(fields.Datetime.now()).astimezone(self._tz()).date()

    def _local_midnight_utc(self):
        """The start of the session's own day, as naive UTC for the ORM."""
        self.ensure_one()
        tz = self._tz()
        local_now = pytz.UTC.localize(fields.Datetime.now()).astimezone(tz)
        midnight = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
        return midnight.astimezone(pytz.UTC).replace(tzinfo=None)

    def _daily_cap(self):
        """Today's allowance: base * growth ** days_online, clamped. 0 = unlimited.

        A curve, not a table. Until the first send there is no start date, so
        day one's allowance applies — which is what a brand new number should get.
        """
        self.ensure_one()
        if not self.warmup_enabled:
            return 0
        start = self.warmup_start_date or self._local_today()
        days = max(0, (self._local_today() - start).days)
        try:
            grown = self.daily_cap_base * (self.daily_cap_growth ** days)
        except OverflowError:  # a long-lived number with a large growth factor
            return self.daily_cap_max
        return max(0, min(self.daily_cap_max, math.ceil(grown)))

    def _hourly_cap(self):
        """A day's allowance must not leave in one hour."""
        self.ensure_one()
        daily = self._daily_cap()
        if self.hourly_cap:
            return self.hourly_cap
        return math.ceil(daily / 12) if daily else 0

    def _sent_since(self, since):
        """Count what this number has actually put on the wire since `since`.

        A query, not a stored counter: a counter needs a reset cron and drifts
        whenever a transaction rolls back. This runs once per cron pass, not
        once per message.
        """
        self.ensure_one()
        return self.env["whatsmeow.message"].search_count([
            ("session_id", "=", self.id),
            ("direction", "=", "out"),
            ("sent_date", ">=", since),
        ])

    @api.depends("warmup_enabled", "warmup_start_date", "daily_cap_base",
                 "daily_cap_growth", "daily_cap_max", "hourly_cap", "tz")
    def _compute_volume(self):
        for rec in self:
            rec.daily_cap = rec._daily_cap()
            rec.sent_today = rec._sent_since(rec._local_midnight_utc())
            rec.sent_this_hour = rec._sent_since(
                fields.Datetime.now() - timedelta(hours=1))

    def _note_send(self):
        """Start the ramp on the first message this number ever sends."""
        self.ensure_one()
        if self.warmup_enabled and not self.warmup_start_date:
            self.warmup_start_date = self._local_today()

    # -- send throttling ------------------------------------------------------
    # Bursting is the main ban lever on the unofficial protocol, so the queue
    # paces sends. The pacing is per session because the risk is per number:
    # one busy number must not hold up another, and two numbers sharing a
    # gateway are still two independent reputations.
    def _seconds_until_sendable(self):
        """How long the queue must wait before this number may send again.

        `None` means "not within this run": the daily allowance is spent (it
        reopens at the session's own midnight) or the hour's is (the queue is
        better off working another number than sleeping here). Callers must
        treat it as "drop this session", not as zero.
        """
        self.ensure_one()
        daily = self._daily_cap()
        if daily:
            if self._sent_since(self._local_midnight_utc()) >= daily:
                return None
            hourly = self._hourly_cap()
            if hourly and self._sent_since(
                    fields.Datetime.now() - timedelta(hours=1)) >= hourly:
                return None
        if not self.next_send_at:
            return 0.0
        delta = (self.next_send_at - fields.Datetime.now()).total_seconds()
        return max(0.0, delta)

    def _schedule_next_send(self):
        """Close this number's send window for a random spell.

        Only the queue calls this: a hand-sent message from the form is a
        human act at human speed, and making the user wait for it would be
        confusing without lowering the risk.
        """
        self.ensure_one()
        delay = random.uniform(self.send_delay_min, self.send_delay_max)
        self.next_send_at = fields.Datetime.now() + timedelta(seconds=delay)

    # -- recipient validation (PLAN.md §12.4) ---------------------------------
    def _check_numbers(self, phones):
        """Ask the gateway which of these numbers are on WhatsApp.

        Returns `{digits: True/False}` for the numbers it answered about — an
        unanswered number is simply absent, never a `False`. WhatsApp does not
        always answer, and the gateway rations the lookups (bulk-querying is
        itself a bulk-sender fingerprint), so treating silence as "not
        registered" would permanently stop us messaging a real contact.
        """
        self.ensure_one()
        digits = [d for d in (DIGITS.sub("", p or "") for p in phones) if d]
        if not digits:
            return {}
        answers = {}
        for start in range(0, len(digits), CHECK_BATCH_SIZE):
            batch = digits[start:start + CHECK_BATCH_SIZE]
            data = self._gw("POST", f"/sessions/{self.code}/check", {"phones": batch})
            for row in data.get("results") or []:
                number = DIGITS.sub("", row.get("number") or "")
                if number:
                    answers[number] = bool(row.get("registered"))
            if data.get("throttled"):
                # The gateway is drip-feeding the lookups on purpose; stop
                # asking and let the next cron pass take the rest.
                _logger.info("whatsmeow: session %s check throttled by the gateway",
                             self.code)
                break
        return answers

    def _gw(self, method, path, payload=None, timeout=None):
        self.ensure_one()
        if timeout is None:
            return self.connection_id._request(method, path, payload)
        return self.connection_id._request(method, path, payload, timeout=timeout)

    def _apply_state(self, data):
        """Write back a gateway status payload, rendering the QR string to a PNG."""
        self.ensure_one()
        vals = {
            "status": data.get("status") or self.status,
            "last_error": data.get("error") or False,
            "jid": data.get("jid") or self.jid,
        }
        qr_string = data.get("qr")
        if qr_string:
            buf = io.BytesIO()
            qrcode.make(qr_string).save(buf, format="PNG")
            vals["qr_image"] = base64.b64encode(buf.getvalue())
        elif vals["status"] != "qr":
            vals["qr_image"] = False
        self.write(vals)

    # -- UI actions -----------------------------------------------------------
    def action_start(self):
        for rec in self:
            rec._apply_state(rec._gw("POST", f"/sessions/{rec.code}/start"))

    def action_refresh(self):
        for rec in self:
            data = rec._gw("GET", f"/sessions/{rec.code}/status")
            if data.get("status") == "qr":
                data.update(rec._gw("GET", f"/sessions/{rec.code}/qr"))
            rec._apply_state(data)

    def action_logout(self):
        for rec in self:
            rec._gw("POST", f"/sessions/{rec.code}/logout")
            rec.write({"status": "logged_out", "qr_image": False, "jid": False})

    @api.model
    def cron_refresh_all(self):
        for rec in self.search([("status", "not in", ("draft", "logged_out"))]):
            try:
                rec.action_refresh()
                rec.env.cr.commit()
            except Exception as exc:  # noqa: BLE001 - one bad gateway must not kill the cron
                rec.env.cr.rollback()
                _logger.warning("whatsmeow.session %s refresh failed: %s", rec.code, exc)
