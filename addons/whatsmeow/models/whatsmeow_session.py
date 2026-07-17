import base64
import io
import logging
import re

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

    def _gw(self, method, path, payload=None):
        self.ensure_one()
        return self.connection_id._request(method, path, payload)

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
