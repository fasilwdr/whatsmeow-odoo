import logging

import requests

from odoo import _, api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

REQUEST_TIMEOUT = 20
# Uploading/downloading media is slower than a status poll; give it room.
MEDIA_TIMEOUT = 300


class WhatsmeowConnection(models.Model):
    _name = "whatsmeow.connection"
    _description = "Whatsmeow Gateway Endpoint"
    _order = "name"

    name = fields.Char(required=True)
    base_url = fields.Char(
        string="Gateway URL", required=True, default="http://127.0.0.1:8080",
        help="Base URL of the whatsmeow gateway, e.g. http://127.0.0.1:8080",
    )
    api_key = fields.Char(
        string="API Key", required=True,
        groups="whatsmeow.group_whatsmeow_manager",
        help="Must match WMG_API_KEY on this gateway.",
    )
    webhook_secret = fields.Char(
        string="Webhook Secret", required=True, index=True,
        groups="whatsmeow.group_whatsmeow_manager",
        help="Must match WMG_WEBHOOK_SECRET on this gateway. Used to route "
             "inbound webhooks back to this connection.",
    )
    active = fields.Boolean(default=True)
    session_ids = fields.One2many("whatsmeow.session", "connection_id")
    session_count = fields.Integer(compute="_compute_session_count")

    _webhook_secret_uniq = models.Constraint(
        "UNIQUE (webhook_secret)",
        "Each gateway connection must use a distinct webhook secret.",
    )

    @api.depends("session_ids")
    def _compute_session_count(self):
        for rec in self:
            rec.session_count = len(rec.session_ids)

    # -- shared HTTP helper ---------------------------------------------------
    def _call(self, method, path, payload=None, timeout=REQUEST_TIMEOUT):
        """Perform the HTTP call and raise on transport or gateway errors.

        Credentials are manager-only fields, so they're read through sudo: any
        user allowed to send a message may use the gateway without ever being
        able to see the key.
        """
        self.ensure_one()
        conn = self.sudo()
        base = (conn.base_url or "").rstrip("/")
        if not base or not conn.api_key:
            raise UserError(_("Connection '%s' is missing a URL or API key.", self.name))
        try:
            resp = requests.request(
                method, f"{base}{path}", json=payload,
                headers={"X-Api-Key": conn.api_key}, timeout=timeout,
            )
        except requests.RequestException as exc:
            raise UserError(_("Gateway '%s' unreachable: %s", self.name, exc)) from exc
        if resp.status_code >= 400:
            # Errors are JSON even on the binary endpoints.
            try:
                detail = resp.json().get("error", resp.text)
            except ValueError:
                detail = resp.text
            raise UserError(_("Gateway error (%s): %s", resp.status_code, detail))
        return resp

    def _request(self, method, path, payload=None, timeout=REQUEST_TIMEOUT):
        """Call the gateway and decode a JSON reply."""
        resp = self._call(method, path, payload, timeout)
        try:
            return resp.json()
        except ValueError:
            return {}

    def _request_raw(self, method, path, payload=None):
        """Call the gateway and return raw bytes + headers, for media downloads.

        Media can be large, so this gets its own, longer timeout.
        """
        resp = self._call(method, path, payload, timeout=MEDIA_TIMEOUT)
        return resp.content, resp.headers

    def action_test(self):
        # Hits an authed endpoint so it validates the API key, not just reachability.
        self.ensure_one()
        self._request("GET", "/sessions")
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "type": "success",
                "sticky": False,
                "message": _("Gateway '%s' reachable and key valid.", self.name),
            },
        }

    def action_view_sessions(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": _("Sessions"),
            "res_model": "whatsmeow.session",
            "view_mode": "list,form",
            "domain": [("connection_id", "=", self.id)],
            "context": {"default_connection_id": self.id},
        }
