import json

from odoo.tests import HttpCase, tagged
from odoo.tools import mute_logger


@tagged("post_install", "-at_install")
class TestWebhookOverHttp(HttpCase):
    """The gateway's side of the contract, over a real HTTP request.

    Every other webhook test calls `_on_message` directly, which proves the
    handling and nothing about the route: the gateway is a separate program
    that only ever sees a URL, a header and a JSON body, so that surface is
    what has to keep working across Odoo versions.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.connection = cls.env["whatsmeow.connection"].create({
            "name": "GW", "base_url": "http://127.0.0.1:8080",
            "api_key": "k", "webhook_secret": "http-secret",
        })
        # Not `cls.session`: that name is the HTTP session of an HttpCase.
        cls.wa_session = cls.env["whatsmeow.session"].create({
            "name": "ACME", "code": "acme-http", "connection_id": cls.connection.id,
        })

    def _post(self, payload, secret="http-secret"):
        headers = {"Content-Type": "application/json"}
        if secret:
            headers["X-Webhook-Secret"] = secret
        with mute_logger("odoo.addons.whatsmeow.controllers.webhook"):
            return self.url_open(
                "/whatsmeow/webhook", data=json.dumps(payload), headers=headers)

    def test_a_message_event_is_accepted_and_stored(self):
        response = self._post({
            "event": "message.received",
            "session": "acme-http",
            "data": {
                "wa_message_id": "HTTP-1",
                "sender_phone": "447700900321",
                "sender_jid": "447700900321@s.whatsapp.net",
                "chat_jid": "447700900321@s.whatsapp.net",
                "body": "over the wire",
            },
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})
        message = self.env["whatsmeow.message"].search(
            [("wa_message_id", "=", "HTTP-1")])
        self.assertEqual(message.body, "over the wire")
        self.assertEqual(message.session_id, self.wa_session)

    def test_a_session_event_updates_the_session(self):
        response = self._post({"event": "session.connected", "session": "acme-http"})
        self.assertEqual(response.status_code, 200)
        self.wa_session.invalidate_recordset(["status"])
        self.assertEqual(self.wa_session.status, "connected")

    def test_a_wrong_secret_is_unauthorized(self):
        self.assertEqual(self._post({}, secret="nope").status_code, 401)
        self.assertEqual(self._post({}, secret=None).status_code, 401)

    def test_an_unknown_session_is_not_found(self):
        response = self._post({"event": "session.connected", "session": "ghost"})
        self.assertEqual(response.status_code, 404)

    def test_a_body_that_is_not_json_is_refused(self):
        with mute_logger("odoo.addons.whatsmeow.controllers.webhook"):
            response = self.url_open(
                "/whatsmeow/webhook", data="{not json",
                headers={"X-Webhook-Secret": "http-secret",
                         "Content-Type": "application/json"})
        self.assertEqual(response.status_code, 400)
