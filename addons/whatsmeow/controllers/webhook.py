import json
import logging

from markupsafe import Markup

from odoo import http
from odoo.http import request

_logger = logging.getLogger(__name__)

SESSION_EVENT_STATUS = {
    "session.paired": "connected",
    "session.connected": "connected",
    "session.disconnected": "disconnected",
    "session.logged_out": "logged_out",
}


class WhatsmeowWebhook(http.Controller):
    """Receives gateway events. Routing is by webhook secret -> connection -> session,
    so several gateways can post to the same Odoo without colliding."""

    @http.route("/whatsmeow/webhook", type="http", auth="public",
                methods=["POST"], csrf=False, save_session=False)
    def webhook(self, **kwargs):
        env = request.env(su=True)
        secret = request.httprequest.headers.get("X-Webhook-Secret")
        connection = env["whatsmeow.connection"].search(
            [("webhook_secret", "=", secret)], limit=1) if secret else None
        if not connection:
            return request.make_json_response({"error": "unauthorized"}, status=401)

        try:
            payload = json.loads(request.httprequest.get_data(as_text=True) or "{}")
        except ValueError:
            return request.make_json_response({"error": "bad json"}, status=400)

        session = env["whatsmeow.session"].search(
            [("code", "=", payload.get("session")),
             ("connection_id", "=", connection.id)], limit=1)
        if not session:
            return request.make_json_response({"error": "unknown session"}, status=404)

        event = payload.get("event")
        data = payload.get("data") or {}

        if event == "message.received":
            self._on_message(env, session, data)
        elif event == "message.receipt":
            self._on_receipt(env, data)
        elif event in SESSION_EVENT_STATUS:
            vals = {"status": SESSION_EVENT_STATUS[event], "qr_image": False}
            if event == "session.logged_out":
                vals["last_error"] = data.get("reason") or "logged out"
            session.write(vals)
        else:
            _logger.info("whatsmeow: ignoring unknown event %r", event)

        return request.make_json_response({"status": "ok"})

    def _on_message(self, env, session, data):
        wa_id = data.get("wa_message_id")
        if wa_id and env["whatsmeow.message"].search_count(
            [("wa_message_id", "=", wa_id), ("direction", "=", "in")], limit=1
        ):
            return  # webhook retries -> stay idempotent
        # sender_phone is empty when WhatsApp only gave us a LID; don't try to
        # match a partner on it, and never store the LID as if it were a phone.
        phone = (data.get("sender_phone") or "").strip()
        partner = env["whatsmeow.message"]._find_partner(phone) if phone else \
            env["res.partner"]
        body = data.get("body") or ""
        env["whatsmeow.message"].create({
            "session_id": session.id,
            "partner_id": partner.id or False,
            "phone": phone,
            "sender_lid": data.get("sender_lid") or False,
            "direction": "in",
            "state": "received",
            "body": body,
            "wa_message_id": wa_id,
        })
        if partner:
            # Markup(...) % args escapes the args: inbound text is untrusted.
            # env._() rather than _(): the bare alias sniffs the caller's frame
            # for a language and blows up when there's no active request.
            partner.message_post(
                body=Markup("<p><b>%s</b><br/>%s</p>") % (
                    env._("WhatsApp (%s)", session.name), body,
                ),
                message_type="comment",
            )

    def _on_receipt(self, env, data):
        state = "read" if data.get("receipt_type") == "read" else "delivered"
        wa_ids = data.get("wa_message_ids") or []
        if not wa_ids:
            return
        env["whatsmeow.message"].search([
            ("wa_message_id", "in", wa_ids),
            ("direction", "=", "out"),
        ]).write({"state": state})
