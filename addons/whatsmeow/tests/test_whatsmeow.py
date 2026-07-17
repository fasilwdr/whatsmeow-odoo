import json
from unittest.mock import patch

from psycopg2 import IntegrityError

from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tests import TransactionCase, tagged
from odoo.tools import mute_logger


class WhatsmeowCommon(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.connection = cls.env["whatsmeow.connection"].create({
            "name": "GW One",
            "base_url": "http://127.0.0.1:8080",
            "api_key": "key-one",
            "webhook_secret": "secret-one",
        })
        cls.session = cls.env["whatsmeow.session"].create({
            "name": "Client ACME",
            "code": "client_acme",
            "connection_id": cls.connection.id,
        })


@tagged("post_install", "-at_install")
class TestConnection(WhatsmeowCommon):

    def test_webhook_secret_is_unique(self):
        """Routing assumes secrets are unique; Odoo 19 silently drops
        _sql_constraints, so assert the models.Constraint really exists."""
        with self.assertRaises(IntegrityError), mute_logger("odoo.sql_db"):
            self.env["whatsmeow.connection"].create({
                "name": "GW Two",
                "base_url": "http://127.0.0.1:8081",
                "api_key": "key-two",
                "webhook_secret": "secret-one",
            })

    def test_session_code_unique_per_connection(self):
        other = self.env["whatsmeow.connection"].create({
            "name": "GW Two",
            "base_url": "http://127.0.0.1:8081",
            "api_key": "key-two",
            "webhook_secret": "secret-two",
        })
        # same code on a different gateway is fine
        self.env["whatsmeow.session"].create({
            "name": "Other", "code": "client_acme", "connection_id": other.id,
        })
        # ...but not twice on the same one
        with self.assertRaises(IntegrityError), mute_logger("odoo.sql_db"):
            self.env["whatsmeow.session"].create({
                "name": "Dup", "code": "client_acme", "connection_id": self.connection.id,
            })

    def test_request_sends_api_key_and_raises_on_error(self):
        with patch("odoo.addons.whatsmeow.models.whatsmeow_connection.requests.request") as req:
            req.return_value.status_code = 200
            req.return_value.json.return_value = []
            self.connection.action_test()
            self.assertEqual(req.call_args.kwargs["headers"]["X-Api-Key"], "key-one")
            self.assertEqual(req.call_args.args[1], "http://127.0.0.1:8080/sessions")

        with patch("odoo.addons.whatsmeow.models.whatsmeow_connection.requests.request") as req:
            req.return_value.status_code = 401
            req.return_value.json.return_value = {"error": "invalid key"}
            with self.assertRaises(UserError):
                self.connection.action_test()

    def test_base_url_trailing_slash_is_normalised(self):
        self.connection.base_url = "http://127.0.0.1:8080/"
        with patch("odoo.addons.whatsmeow.models.whatsmeow_connection.requests.request") as req:
            req.return_value.status_code = 200
            req.return_value.json.return_value = []
            self.connection.action_test()
            self.assertEqual(req.call_args.args[1], "http://127.0.0.1:8080/sessions")


@tagged("post_install", "-at_install")
class TestSession(WhatsmeowCommon):

    def test_invalid_code_rejected(self):
        for bad in ("Client ACME", "UPPER", "with.dot", "", "x" * 41):
            with self.assertRaises(ValidationError, msg=f"{bad!r} should be rejected"):
                self.env["whatsmeow.session"].create({
                    "name": "Bad", "code": bad, "connection_id": self.connection.id,
                })

    def test_apply_state_renders_qr_png(self):
        self.session._apply_state({"status": "qr", "qr": "2@abcdef"})
        self.assertEqual(self.session.status, "qr")
        self.assertTrue(self.session.qr_image)
        import base64
        self.assertTrue(base64.b64decode(self.session.qr_image).startswith(b"\x89PNG"))

    def test_apply_state_clears_qr_once_connected(self):
        self.session._apply_state({"status": "qr", "qr": "2@abcdef"})
        self.session._apply_state({"status": "connected", "jid": "44770@s.whatsapp.net"})
        self.assertEqual(self.session.status, "connected")
        self.assertFalse(self.session.qr_image)
        self.assertEqual(self.session.jid, "44770@s.whatsapp.net")


@tagged("post_install", "-at_install")
class TestMessage(WhatsmeowCommon):

    def test_find_partner_matches_across_formatting(self):
        """The inbound webhook only ever gives us bare digits; partners store
        whatever a human typed. Every one of these must resolve."""
        for stored in ("+44 7700 900123", "447700900123", "+44-7700-900123",
                       "(0044) 7700 900123"):
            partner = self.env["res.partner"].create({"name": "Alice", "phone": stored})
            found = self.env["whatsmeow.message"]._find_partner("447700900123")
            self.assertEqual(found, partner, f"failed for stored phone {stored!r}")
            partner.unlink()

    def test_find_partner_no_match_returns_empty(self):
        self.env["res.partner"].create({"name": "Bob", "phone": "+44 7700 111222"})
        self.assertFalse(self.env["whatsmeow.message"]._find_partner("447700900123"))

    def test_find_partner_empty_phone(self):
        self.assertFalse(self.env["whatsmeow.message"]._find_partner(""))
        self.assertFalse(self.env["whatsmeow.message"]._find_partner(None))

    def test_send_success_and_failure(self):
        msg = self.env["whatsmeow.message"].create({
            "session_id": self.session.id, "phone": "447700900123",
            "direction": "out", "body": "hi",
        })
        with patch.object(type(self.session), "_gw", return_value={"wa_message_id": "3EB0"}):
            msg.action_send()
        self.assertEqual(msg.state, "sent")
        self.assertEqual(msg.wa_message_id, "3EB0")

        msg2 = self.env["whatsmeow.message"].create({
            "session_id": self.session.id, "phone": "447700900124",
            "direction": "out", "body": "hi",
        })
        with patch.object(type(self.session), "_gw", side_effect=UserError("boom")), \
                mute_logger("odoo.addons.whatsmeow.models.whatsmeow_message"):
            msg2.action_send()
        self.assertEqual(msg2.state, "error")
        self.assertIn("boom", msg2.error_message)

    def test_cron_only_picks_queued_outgoing(self):
        sent = self.env["whatsmeow.message"].create({
            "session_id": self.session.id, "phone": "447700900123",
            "direction": "out", "body": "already", "state": "sent",
        })
        with patch.object(type(self.session), "_gw", return_value={"wa_message_id": "X"}) as gw:
            self.env["whatsmeow.message"].cron_process_outgoing()
        self.assertEqual(sent.state, "sent")
        self.assertEqual(gw.call_count, 0)


@tagged("post_install", "-at_install")
class TestSecurity(WhatsmeowCommon):

    def setUp(self):
        super().setUp()
        self.user = self.env["res.users"].create({
            "name": "Plain User", "login": "wm_user",
            "group_ids": [(6, 0, [
                self.env.ref("base.group_user").id,
                self.env.ref("whatsmeow.group_whatsmeow_user").id,
            ])],
        })

    def test_user_cannot_read_secrets(self):
        conn = self.connection.with_user(self.user)
        with self.assertRaises(AccessError):
            conn.api_key  # noqa: B018 - reading is the assertion
        with self.assertRaises(AccessError):
            conn.webhook_secret  # noqa: B018

    def test_user_can_send_without_seeing_the_key(self):
        """_request() sudo's to read credentials, so a plain user can send."""
        msg = self.env["whatsmeow.message"].with_user(self.user).create({
            "session_id": self.session.id, "phone": "447700900123",
            "direction": "out", "body": "hi",
        })
        with patch("odoo.addons.whatsmeow.models.whatsmeow_connection.requests.request") as req:
            req.return_value.status_code = 200
            req.return_value.json.return_value = {"wa_message_id": "3EB0"}
            msg.action_send()
        self.assertEqual(msg.state, "sent")
        self.assertEqual(req.call_args.kwargs["headers"]["X-Api-Key"], "key-one")

    def test_user_cannot_write_connection(self):
        with self.assertRaises(AccessError):
            self.connection.with_user(self.user).write({"name": "hacked"})


@tagged("post_install", "-at_install")
class TestWebhookRouting(WhatsmeowCommon):

    def test_receipt_updates_outgoing_state(self):
        msg = self.env["whatsmeow.message"].create({
            "session_id": self.session.id, "phone": "447700900123",
            "direction": "out", "body": "hi", "state": "sent",
            "wa_message_id": "3EB0",
        })
        from odoo.addons.whatsmeow.controllers.webhook import WhatsmeowWebhook
        WhatsmeowWebhook()._on_receipt(self.env, {
            "receipt_type": "read", "wa_message_ids": ["3EB0"],
        })
        self.assertEqual(msg.state, "read")

    def test_inbound_message_is_idempotent_and_escapes_body(self):
        from odoo.addons.whatsmeow.controllers.webhook import WhatsmeowWebhook
        partner = self.env["res.partner"].create({
            "name": "Alice", "phone": "+44 7700 900123",
        })
        data = {
            "wa_message_id": "3EB0IN",
            "sender_phone": "447700900123",
            "body": "<img src=x onerror=alert(1)>",
        }
        ctrl = WhatsmeowWebhook()
        ctrl._on_message(self.env, self.session, data)
        ctrl._on_message(self.env, self.session, data)  # retry

        msgs = self.env["whatsmeow.message"].search([("wa_message_id", "=", "3EB0IN")])
        self.assertEqual(len(msgs), 1, "webhook retry must not duplicate")
        self.assertEqual(msgs.partner_id, partner)
        self.assertEqual(msgs.state, "received")

        post = self.env["mail.message"].search(
            [("model", "=", "res.partner"), ("res_id", "=", partner.id)],
            order="id desc", limit=1,
        )
        self.assertNotIn("<img", post.body)
        self.assertIn("&lt;img", post.body)
