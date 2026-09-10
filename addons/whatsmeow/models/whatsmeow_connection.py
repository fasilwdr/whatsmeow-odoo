import logging

import requests

from odoo import _, api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

# Must match the route in controllers/webhook.py.
WEBHOOK_PATH = "/whatsmeow/webhook"

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
    callback_base_url = fields.Char(
        string="This Odoo's URL", groups="whatsmeow.group_whatsmeow_manager",
        help="Where this Odoo is reachable *from the gateway*, e.g. "
             "https://acme.example.com. Sent to the gateway when a session "
             "starts, so one gateway can serve several Odoo databases. Leave "
             "empty to use the system's Web Base URL.",
    )
    webhook_url = fields.Char(
        compute="_compute_webhook_url", string="Webhook URL",
        help="The address the gateway posts this connection's events to.",
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

    @api.depends("callback_base_url")
    def _compute_webhook_url(self):
        """Where this Odoo wants the gateway to post.

        The system parameter is the sane default, but it is wrong often enough
        — behind a proxy, on a staging clone restored from production — that
        the connection must be able to override it. Read through sudo because
        `callback_base_url` is a manager-only field and any user may send.
        """
        default = self.env["ir.config_parameter"].sudo().get_param("web.base.url") or ""
        for rec in self:
            base = (rec.sudo().callback_base_url or default).strip().rstrip("/")
            rec.webhook_url = f"{base}{WEBHOOK_PATH}" if base else False

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
        # Hits an authed endpoint so it validates the API key, not just
        # reachability. The gateway answers with the sessions *this key* owns,
        # which is also the quickest way to spot a key pasted into the wrong
        # database: the reply lists somebody else's numbers, or none at all.
        self.ensure_one()
        sessions = self._request("GET", "/sessions") or []
        names = ", ".join(s.get("session", "?") for s in sessions) or _("none yet")
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "type": "success",
                "sticky": False,
                "message": _("Gateway '%(name)s' reachable and key valid. "
                             "Sessions on this key: %(sessions)s.",
                             name=self.name, sessions=names),
                "next": {"type": "ir.actions.act_window_close"},
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
