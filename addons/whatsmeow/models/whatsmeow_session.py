import base64
import io
import logging
import random
import re
from datetime import timedelta

import qrcode

from odoo import _, api, fields, models
from odoo.exceptions import ValidationError

_logger = logging.getLogger(__name__)

# Must stay in sync with sessionNameRe in gateway/main.go.
SESSION_CODE_RE = re.compile(r"^[a-z0-9_-]{1,40}$")


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
        for rule in self.inbound_rule_ids.sorted(key=lambda r: (r.sequence, r.id)):
            if rule._matches(facts):
                return rule.action
        return self.inbound_default

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

    # -- send throttling ------------------------------------------------------
    # Bursting is the main ban lever on the unofficial protocol, so the queue
    # paces sends. The pacing is per session because the risk is per number:
    # one busy number must not hold up another, and two numbers sharing a
    # gateway are still two independent reputations.
    def _seconds_until_sendable(self):
        """How long the queue must wait before this number may send again."""
        self.ensure_one()
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
