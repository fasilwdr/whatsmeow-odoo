import base64
import re
from datetime import timedelta
from unittest.mock import patch

from psycopg2 import IntegrityError

from odoo import fields
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

    def test_outgoing_requires_a_phone(self):
        with self.assertRaises(ValidationError):
            self.env["whatsmeow.message"].create({
                "session_id": self.session.id, "direction": "out", "body": "hi",
            })

    def test_incoming_may_have_no_phone(self):
        """WhatsApp does not always reveal the phone behind a LID; such a message
        must still be logged rather than rejected."""
        msg = self.env["whatsmeow.message"].create({
            "session_id": self.session.id, "direction": "in", "state": "received",
            "body": "hi", "sender_lid": "126864760766535",
        })
        self.assertFalse(msg.phone)
        self.assertEqual(msg.sender_lid, "126864760766535")

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
class TestMedia(WhatsmeowCommon):

    PNG = base64.b64encode(
        b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
    )

    def test_kind_inferred_from_mimetype(self):
        msg = self.env["whatsmeow.message"]
        cases = {
            "image/jpeg": "image",
            "image/webp": "sticker",   # webp is how WhatsApp does stickers
            "video/mp4": "video",
            "audio/ogg; codecs=opus": "audio",
            "application/pdf": "document",
            "": "document",
        }
        for mimetype, kind in cases.items():
            self.assertEqual(msg._kind_for_mimetype(mimetype), kind, f"for {mimetype!r}")

    def test_outgoing_media_needs_a_file(self):
        with self.assertRaises(ValidationError):
            self.env["whatsmeow.message"].create({
                "session_id": self.session.id, "phone": "447700900123",
                "direction": "out", "message_type": "image", "body": "caption only",
            })

    def test_outgoing_text_needs_a_body(self):
        with self.assertRaises(ValidationError):
            self.env["whatsmeow.message"].create({
                "session_id": self.session.id, "phone": "447700900123",
                "direction": "out", "message_type": "text",
            })

    def test_media_message_needs_no_body(self):
        """A photo with no caption is perfectly normal."""
        msg = self.env["whatsmeow.message"].create({
            "session_id": self.session.id, "phone": "447700900123",
            "direction": "out", "message_type": "image",
            "media_data": self.PNG, "media_filename": "x.png",
        })
        self.assertFalse(msg.body)

    def test_send_payload_routes_text_and_media_differently(self):
        text = self.env["whatsmeow.message"].create({
            "session_id": self.session.id, "phone": "447700900123",
            "direction": "out", "body": "hi",
        })
        path, payload = text._send_payload()
        self.assertEqual(path, "/sessions/client_acme/send")
        self.assertEqual(payload["message"], "hi")
        self.assertNotIn("data", payload)

        media = self.env["whatsmeow.message"].create({
            "session_id": self.session.id, "phone": "447700900123",
            "direction": "out", "message_type": "document", "body": "the invoice",
            "media_data": self.PNG, "media_filename": "invoice.pdf",
            "media_mimetype": "application/pdf",
        })
        path, payload = media._send_payload()
        self.assertEqual(path, "/sessions/client_acme/send-media")
        self.assertEqual(payload["kind"], "document")
        self.assertEqual(payload["caption"], "the invoice")
        self.assertEqual(payload["filename"], "invoice.pdf")
        # data must be base64 the gateway can decode straight back to bytes
        self.assertTrue(base64.b64decode(payload["data"]).startswith(b"\x89PNG"))

    def test_send_media_uses_the_media_endpoint(self):
        msg = self.env["whatsmeow.message"].create({
            "session_id": self.session.id, "phone": "447700900123",
            "direction": "out", "message_type": "image",
            "media_data": self.PNG, "media_filename": "x.png",
            "media_mimetype": "image/png",
        })
        with patch.object(type(self.session), "_gw", return_value={"wa_message_id": "M1"}) as gw:
            msg.action_send()
        self.assertEqual(msg.state, "sent")
        self.assertIn("/send-media", gw.call_args.args[1])

    def test_fetch_media_stores_bytes_and_releases_the_gateway(self):
        msg = self.env["whatsmeow.message"].create({
            "session_id": self.session.id, "phone": "447700900123",
            "direction": "in", "state": "received", "message_type": "image",
            "media_filename": "photo.jpg", "media_mimetype": "image/jpeg",
            "media_state": "pending", "wa_message_id": "IN-MEDIA-1",
        })
        raw = base64.b64decode(self.PNG)
        with patch.object(type(self.connection), "_request_raw",
                          return_value=(raw, {})) as fetch, \
                patch.object(type(self.session), "_gw", return_value={}) as gw:
            msg.action_fetch_media()

        self.assertEqual(msg.media_state, "fetched")
        self.assertEqual(base64.b64decode(msg.media_data), raw)
        self.assertEqual(msg.media_size, len(raw))
        self.assertIn("/media/IN-MEDIA-1", fetch.call_args.args[1])
        # the gateway should be told it can drop the file
        self.assertEqual(gw.call_args.args[0], "DELETE")

    def test_fetch_media_failure_is_recorded_not_raised(self):
        msg = self.env["whatsmeow.message"].create({
            "session_id": self.session.id, "phone": "447700900123",
            "direction": "in", "state": "received", "message_type": "image",
            "media_state": "pending", "wa_message_id": "IN-MEDIA-2",
        })
        with patch.object(type(self.connection), "_request_raw",
                          side_effect=UserError("gone")), \
                mute_logger("odoo.addons.whatsmeow.models.whatsmeow_message"):
            msg.action_fetch_media()
        self.assertEqual(msg.media_state, "error")
        self.assertIn("gone", msg.error_message)

    def test_inbound_media_posts_to_chatter_with_attachment(self):
        partner = self.env["res.partner"].create({
            "name": "Media Contact", "phone": "+966 55 019 9013",
        })
        msg = self.env["whatsmeow.message"].create({
            "session_id": self.session.id, "phone": "966550199013",
            "partner_id": partner.id,
            "direction": "in", "state": "received", "message_type": "image",
            "media_filename": "photo.jpg", "media_mimetype": "image/jpeg",
            "media_state": "pending", "wa_message_id": "IN-MEDIA-3",
            "body": "look at this",
        })
        raw = base64.b64decode(self.PNG)
        with patch.object(type(self.connection), "_request_raw", return_value=(raw, {})), \
                patch.object(type(self.session), "_gw", return_value={}):
            msg.action_fetch_media()

        post = self.env["mail.message"].search(
            [("model", "=", "res.partner"), ("res_id", "=", partner.id)],
            order="id desc", limit=1)
        self.assertIn("look at this", post.body)
        self.assertEqual(len(post.attachment_ids), 1)
        self.assertEqual(post.attachment_ids.name, "photo.jpg")
        self.assertEqual(post.attachment_ids.mimetype, "image/jpeg")


@tagged("post_install", "-at_install")
class TestReply(WhatsmeowCommon):
    """Replying has to be addressed to the *chat*. A phone number only ever
    reaches a private chat, so it cannot answer a group, and a LID-only sender
    has no phone at all."""

    GROUP_JID = "120363000000000000@g.us"

    def _incoming_group_message(self):
        return self.env["whatsmeow.message"].create({
            "session_id": self.session.id, "direction": "in", "state": "received",
            "phone": "447700900123",
            "sender_jid": "447700900123@s.whatsapp.net",
            "chat_jid": self.GROUP_JID, "chat_name": "Site Team",
            "body": "who is bringing the keys?", "wa_message_id": "GRP-1",
        })

    def test_chat_type_is_derived_from_the_jid_server(self):
        cases = [
            ("447700900123@s.whatsapp.net", "private"),
            ("35274583240901@lid", "private"),
            (self.GROUP_JID, "group"),
            ("status@broadcast", "status"),
            ("1234@broadcast", "broadcast"),
            ("120363000000000000@newsletter", "newsletter"),
            ("nonsense@example.org", "unknown"),
            ("", "private"),  # addressed by phone: can only be a private chat
        ]
        for jid, expected in cases:
            msg = self.env["whatsmeow.message"].create({
                "session_id": self.session.id, "direction": "in",
                "state": "received", "chat_jid": jid, "body": "x",
            })
            self.assertEqual(msg.chat_type, expected, f"chat_jid {jid!r}")

    def test_group_reply_goes_to_the_group_not_the_participant(self):
        """The regression this guards: addressing the reply to the participant's
        phone would quietly send a private message instead."""
        source = self._incoming_group_message()
        self.assertTrue(source.can_reply)
        action = source.action_reply()
        ctx = action["context"]
        self.assertEqual(ctx["default_chat_jid"], self.GROUP_JID)
        self.assertEqual(ctx["default_phone"], "")
        self.assertFalse(ctx["default_partner_id"])
        self.assertEqual(ctx["default_reply_to_id"], source.id)

        reply = self.env["whatsmeow.message"].create({
            "session_id": self.session.id, "direction": "out",
            "chat_jid": ctx["default_chat_jid"], "phone": ctx["default_phone"],
            "reply_to_id": ctx["default_reply_to_id"], "body": "I have them",
        })
        _path, payload = reply._send_payload()
        self.assertEqual(payload["jid"], self.GROUP_JID)
        self.assertEqual(payload["quoted_id"], "GRP-1")
        self.assertEqual(payload["quoted_participant"], "447700900123@s.whatsapp.net")
        self.assertEqual(payload["quoted_text"], "who is bringing the keys?")

    def test_reply_to_a_lid_only_sender_needs_no_phone(self):
        source = self.env["whatsmeow.message"].create({
            "session_id": self.session.id, "direction": "in", "state": "received",
            "phone": "", "sender_lid": "126864760766535",
            "sender_jid": "126864760766535@lid", "chat_jid": "126864760766535@lid",
            "body": "hello", "wa_message_id": "LID-1",
        })
        self.assertEqual(source.chat_type, "private")
        self.assertTrue(source.can_reply, "a LID-only sender is still replyable")

        reply = self.env["whatsmeow.message"].create({
            "session_id": self.session.id, "direction": "out",
            "chat_jid": "126864760766535@lid", "body": "hi back",
        })
        _path, payload = reply._send_payload()
        self.assertEqual(payload["jid"], "126864760766535@lid")

    def test_private_reply_keeps_the_phone_and_the_contact(self):
        partner = self.env["res.partner"].create({
            "name": "Reply Contact", "phone": "+966 55 019 9014",
        })
        source = self.env["whatsmeow.message"].create({
            "session_id": self.session.id, "direction": "in", "state": "received",
            "phone": "966550199014", "partner_id": partner.id,
            "chat_jid": "966550199014@s.whatsapp.net", "body": "hi",
            "wa_message_id": "PRIV-1",
        })
        ctx = source.action_reply()["context"]
        self.assertEqual(ctx["default_phone"], "966550199014")
        self.assertEqual(ctx["default_partner_id"], partner.id)

    def test_outgoing_accepts_a_chat_instead_of_a_phone(self):
        msg = self.env["whatsmeow.message"].create({
            "session_id": self.session.id, "direction": "out",
            "chat_jid": self.GROUP_JID, "body": "hello group",
        })
        self.assertEqual(msg.state, "outgoing")

    def test_outgoing_still_needs_some_address(self):
        with self.assertRaises(ValidationError):
            self.env["whatsmeow.message"].create({
                "session_id": self.session.id, "direction": "out", "body": "hi",
            })

    def test_cannot_send_to_a_receive_only_chat(self):
        for jid in ("status@broadcast", "120363000000000000@newsletter"):
            with self.assertRaises(ValidationError), self.cr.savepoint():
                self.env["whatsmeow.message"].create({
                    "session_id": self.session.id, "direction": "out",
                    "chat_jid": jid, "body": "hi",
                })

    def test_cannot_reply_to_a_receive_only_chat(self):
        status = self.env["whatsmeow.message"].create({
            "session_id": self.session.id, "direction": "in", "state": "received",
            "chat_jid": "status@broadcast", "body": "a status update",
        })
        self.assertFalse(status.can_reply)
        with self.assertRaises(UserError):
            status.action_reply()

    def test_a_message_that_is_not_a_reply_carries_no_quote(self):
        msg = self.env["whatsmeow.message"].create({
            "session_id": self.session.id, "direction": "out",
            "phone": "447700900123", "body": "hi",
        })
        _path, payload = msg._send_payload()
        self.assertNotIn("quoted_id", payload)
        self.assertEqual(payload["phone"], "447700900123")
        self.assertNotIn("jid", payload)

    def test_quoting_media_falls_back_to_the_filename(self):
        source = self.env["whatsmeow.message"].create({
            "session_id": self.session.id, "direction": "in", "state": "received",
            "phone": "447700900123", "message_type": "image", "body": "",
            "media_filename": "photo.jpg", "media_mimetype": "image/jpeg",
            "media_state": "fetched", "wa_message_id": "MEDIA-Q1",
            "chat_jid": self.GROUP_JID,
        })
        reply = self.env["whatsmeow.message"].create({
            "session_id": self.session.id, "direction": "out",
            "chat_jid": self.GROUP_JID, "reply_to_id": source.id, "body": "nice",
        })
        _path, payload = reply._send_payload()
        self.assertEqual(payload["quoted_text"], "photo.jpg")

    def test_webhook_records_the_chat_so_a_reply_can_find_it(self):
        from odoo.addons.whatsmeow.controllers.webhook import WhatsmeowWebhook
        WhatsmeowWebhook()._on_message(self.env, self.session, {
            "wa_message_id": "GRP-WH-1",
            "sender_phone": "447700900123",
            "sender_jid": "447700900123@s.whatsapp.net",
            "is_group": True,
            "chat_jid": self.GROUP_JID,
            "chat_name": "Site Team",
            "body": "morning all",
        })
        msg = self.env["whatsmeow.message"].search([("wa_message_id", "=", "GRP-WH-1")])
        self.assertEqual(msg.chat_jid, self.GROUP_JID)
        self.assertEqual(msg.chat_name, "Site Team")
        self.assertEqual(msg.chat_type, "group")
        self.assertEqual(msg.sender_jid, "447700900123@s.whatsapp.net")
        self.assertTrue(msg.can_reply)

    def test_group_message_is_named_in_the_chatter(self):
        """Posted on a participant's chatter, a group message would otherwise
        read as though they had messaged us privately."""
        partner = self.env["res.partner"].create({
            "name": "Group Member", "phone": "+966 55 019 9015",
        })
        msg = self.env["whatsmeow.message"].create({
            "session_id": self.session.id, "direction": "in", "state": "received",
            "phone": "966550199015", "partner_id": partner.id,
            "chat_jid": self.GROUP_JID, "chat_name": "Site Team",
            "body": "morning all", "wa_message_id": "GRP-CHAT-1",
        })
        msg._post_to_chatter()
        post = self.env["mail.message"].search(
            [("model", "=", "res.partner"), ("res_id", "=", partner.id)],
            order="id desc", limit=1)
        self.assertIn("Site Team", post.body)
        self.assertIn("morning all", post.body)

    def test_chatter_post_is_typed_as_whatsapp(self):
        """The badge and the tinted bubble are driven by `message_type`, so the
        post has to carry it: as a plain 'comment' it would be indistinguishable
        from an ordinary note in the thread."""
        partner = self.env["res.partner"].create({
            "name": "Typed Contact", "phone": "+966 55 019 9016",
        })
        msg = self.env["whatsmeow.message"].create({
            "session_id": self.session.id, "direction": "in", "state": "received",
            "phone": "966550199016", "partner_id": partner.id,
            "body": "hello", "wa_message_id": "TYPED-1",
        })
        msg._post_to_chatter()
        post = self.env["mail.message"].search(
            [("model", "=", "res.partner"), ("res_id", "=", partner.id)],
            order="id desc", limit=1)
        self.assertEqual(post.message_type, "whatsmeow")
        # ...and still a discussion, so followers are notified as before.
        self.assertEqual(post.subtype_id, self.env.ref("mail.mt_comment"))


@tagged("post_install", "-at_install")
class TestInboundDedup(WhatsmeowCommon):
    """The gateway retries, and WhatsApp itself delivers some messages twice."""

    def setUp(self):
        super().setUp()
        from odoo.addons.whatsmeow.controllers.webhook import WhatsmeowWebhook
        self.ctrl = WhatsmeowWebhook()

    def _messages(self, wa_id):
        return self.env["whatsmeow.message"].search([("wa_message_id", "=", wa_id)])

    def test_duplicate_wa_id_is_rejected_by_the_database(self):
        """A search-then-create cannot survive concurrent retries, so the
        guarantee has to live in the index, not the Python."""
        self.env["whatsmeow.message"].create({
            "session_id": self.session.id, "direction": "in", "state": "received",
            "body": "first", "wa_message_id": "3EBDUP",
        })
        with self.assertRaises(IntegrityError), mute_logger("odoo.sql_db"):
            self.env["whatsmeow.message"].create({
                "session_id": self.session.id, "direction": "in", "state": "received",
                "body": "second", "wa_message_id": "3EBDUP",
            })
            self.env.flush_all()

    def test_outgoing_may_share_a_wa_id_with_inbound(self):
        """The index is inbound-only: it must not police the outgoing log."""
        self.env["whatsmeow.message"].create({
            "session_id": self.session.id, "direction": "in", "state": "received",
            "body": "in", "wa_message_id": "3EBSHARED",
        })
        out = self.env["whatsmeow.message"].create({
            "session_id": self.session.id, "direction": "out", "phone": "447700900123",
            "body": "out", "wa_message_id": "3EBSHARED",
        })
        self.env.flush_all()
        self.assertTrue(out.id)

    def test_many_outgoing_may_have_no_wa_id_yet(self):
        """A queued message has no id until the gateway answers; they must not
        all collide on NULL."""
        for i in range(3):
            self.env["whatsmeow.message"].create({
                "session_id": self.session.id, "direction": "out",
                "phone": "447700900123", "body": f"queued {i}",
            })
        self.env.flush_all()
        self.assertEqual(len(self.env["whatsmeow.message"].search(
            [("state", "=", "outgoing"), ("wa_message_id", "=", False)])), 3)

    def test_plain_retry_creates_one_record(self):
        data = {"wa_message_id": "3EBR", "sender_phone": "447700900123", "body": "hi"}
        self.ctrl._on_message(self.env, self.session, data)
        self.ctrl._on_message(self.env, self.session, data)
        self.assertEqual(len(self._messages("3EBR")), 1)

    def test_real_copy_replaces_the_empty_one(self):
        """WhatsApp delivers some messages twice, the first copy empty. Keeping
        whichever landed first would keep the stub and lose the actual text."""
        self.ctrl._on_message(self.env, self.session, {
            "wa_message_id": "3EBTWIN", "sender_phone": "447700900123",
            "body": "[unsupported message type: text]", "placeholder": True,
        })
        stub = self._messages("3EBTWIN")
        self.assertTrue(stub.is_placeholder)

        self.ctrl._on_message(self.env, self.session, {
            "wa_message_id": "3EBTWIN", "sender_phone": "447700900123",
            "body": "Undoo", "placeholder": False,
        })
        merged = self._messages("3EBTWIN")
        self.assertEqual(len(merged), 1, "the real copy must not add a second row")
        self.assertEqual(merged.body, "Undoo")
        self.assertFalse(merged.is_placeholder)

    def test_a_real_copy_is_never_downgraded_to_a_stub(self):
        """The stub can arrive second; it must not overwrite the real text."""
        self.ctrl._on_message(self.env, self.session, {
            "wa_message_id": "3EBORDER", "sender_phone": "447700900123",
            "body": "Vc available", "placeholder": False,
        })
        self.ctrl._on_message(self.env, self.session, {
            "wa_message_id": "3EBORDER", "sender_phone": "447700900123",
            "body": "[unsupported message type: text]", "placeholder": True,
        })
        self.assertEqual(self._messages("3EBORDER").body, "Vc available")

    def test_inbound_quote_links_back_to_our_outgoing(self):
        """A contact usually replies to something *we* sent, so the lookup has
        to span both directions."""
        out = self.env["whatsmeow.message"].create({
            "session_id": self.session.id, "direction": "out",
            "phone": "447700900123", "body": "your order is ready",
            "wa_message_id": "OUT-9",
        })
        self.ctrl._on_message(self.env, self.session, {
            "wa_message_id": "3EBQ", "sender_phone": "447700900123",
            "body": "on my way", "quoted_id": "OUT-9",
        })
        self.assertEqual(self._messages("3EBQ").reply_to_id, out)

    def test_the_quote_arrives_with_the_real_copy_not_the_stub(self):
        """The empty first copy carries no ContextInfo, so the link can only be
        made when the real one lands."""
        self.ctrl._on_message(self.env, self.session, {
            "wa_message_id": "3EBQTWIN", "sender_phone": "447700900123",
            "body": "[unsupported message type: text]", "placeholder": True,
        })
        first = self.env["whatsmeow.message"].create({
            "session_id": self.session.id, "direction": "in", "state": "received",
            "body": "the question", "wa_message_id": "IN-8",
        })
        self.ctrl._on_message(self.env, self.session, {
            "wa_message_id": "3EBQTWIN", "sender_phone": "447700900123",
            "body": "the answer", "placeholder": False, "quoted_id": "IN-8",
        })
        self.assertEqual(self._messages("3EBQTWIN").reply_to_id, first)

    def test_a_quote_of_a_message_we_never_stored_is_ignored(self):
        self.ctrl._on_message(self.env, self.session, {
            "wa_message_id": "3EBQMISS", "sender_phone": "447700900123",
            "body": "about that", "quoted_id": "NEVER-SEEN",
        })
        msg = self._messages("3EBQMISS")
        self.assertTrue(msg, "the reply itself must still be stored")
        self.assertFalse(msg.reply_to_id)

    def test_real_media_copy_upgrades_the_stub_and_queues_the_download(self):
        self.ctrl._on_message(self.env, self.session, {
            "wa_message_id": "3EBMED", "sender_phone": "447700900123",
            "body": "[unsupported message type: image]", "placeholder": True,
        })
        self.ctrl._on_message(self.env, self.session, {
            "wa_message_id": "3EBMED", "sender_phone": "447700900123", "body": "",
            "placeholder": False,
            "media": {"kind": "image", "mimetype": "image/jpeg",
                      "filename": "p.jpg", "size": 10},
        })
        msg = self._messages("3EBMED")
        self.assertEqual(len(msg), 1)
        self.assertEqual(msg.message_type, "image")
        self.assertEqual(msg.media_state, "pending",
                         "the upgraded record must still get its bytes fetched")


@tagged("post_install", "-at_install")
class TestSendIdempotency(WhatsmeowCommon):

    def test_key_is_stable_across_attempts(self):
        """The whole point: the key must survive the rollback that causes the
        resend, so it cannot be generated per attempt."""
        msg = self.env["whatsmeow.message"].create({
            "session_id": self.session.id, "phone": "447700900123",
            "direction": "out", "body": "hi",
        })
        self.assertEqual(msg._idempotency_key(), msg._idempotency_key())
        self.assertIn(str(msg.id), msg._idempotency_key())

    def test_two_messages_get_different_keys(self):
        common = {"session_id": self.session.id, "phone": "447700900123",
                  "direction": "out", "body": "hi"}
        a = self.env["whatsmeow.message"].create(common)
        b = self.env["whatsmeow.message"].create(common)
        self.assertNotEqual(a._idempotency_key(), b._idempotency_key())

    def test_send_payload_carries_the_key_on_both_endpoints(self):
        text = self.env["whatsmeow.message"].create({
            "session_id": self.session.id, "phone": "447700900123",
            "direction": "out", "body": "hi",
        })
        _, payload = text._send_payload()
        self.assertEqual(payload["idempotency_key"], text._idempotency_key())

        media = self.env["whatsmeow.message"].create({
            "session_id": self.session.id, "phone": "447700900123",
            "direction": "out", "message_type": "image",
            "media_data": base64.b64encode(b"x"), "media_filename": "p.jpg",
        })
        path, payload = media._send_payload()
        self.assertIn("send-media", path)
        self.assertEqual(payload["idempotency_key"], media._idempotency_key())


@tagged("post_install", "-at_install")
class TestThrottle(WhatsmeowCommon):
    """Bursting gets numbers banned, so the queue paces itself per session."""

    def _queue(self, session, body="hi", phone="447700900123"):
        return self.env["whatsmeow.message"].create({
            "session_id": session.id, "phone": phone,
            "direction": "out", "body": body,
        })

    def test_delays_must_be_a_sane_range(self):
        with self.assertRaises(ValidationError):
            self.session.send_delay_min = 30  # default max is 10
        with self.assertRaises(ValidationError):
            self.session.write({"send_delay_min": -1, "send_delay_max": 5})

    def test_queue_closes_the_window_after_a_send(self):
        self.session.write({"send_delay_min": 3, "send_delay_max": 10})
        msg = self._queue(self.session)
        before = fields.Datetime.now()
        with patch.object(type(self.session), "_gw", return_value={"wa_message_id": "A"}):
            self.env["whatsmeow.message"].cron_process_outgoing()
        self.assertEqual(msg.state, "sent")
        self.assertTrue(self.session.next_send_at > before)
        self.assertLessEqual(
            (self.session.next_send_at - before).total_seconds(), 10,
            "the window must not close for longer than send_delay_max",
        )

    def test_queue_waits_for_a_closed_window(self):
        """A number that just sent is left alone until its window reopens."""
        self.session.next_send_at = fields.Datetime.now() + timedelta(minutes=5)
        msg = self._queue(self.session)
        with patch.object(type(self.session), "_gw") as gw:
            self.env["whatsmeow.message"].cron_process_outgoing()
        self.assertEqual(gw.call_count, 0)
        self.assertEqual(msg.state, "outgoing", "the message stays queued for a later run")

    def test_a_throttled_session_does_not_stall_the_others(self):
        """The risk is per number, so one paused number must not hold up another."""
        other = self.env["whatsmeow.session"].create({
            "name": "Client Beta", "code": "client_beta",
            "connection_id": self.connection.id,
            "send_delay_min": 0, "send_delay_max": 0,
        })
        self.session.next_send_at = fields.Datetime.now() + timedelta(minutes=5)
        stalled = self._queue(self.session)
        free = self._queue(other)
        with patch.object(type(self.session), "_gw", return_value={"wa_message_id": "B"}):
            self.env["whatsmeow.message"].cron_process_outgoing()
        self.assertEqual(free.state, "sent")
        self.assertEqual(stalled.state, "outgoing")

    def test_queue_drains_a_backlog_once_paced(self):
        # Warm-up off: this is about pacing, and a new number's default
        # allowance (§12.2) would bound the backlog before the pacing did.
        self.session.write({"send_delay_min": 0, "send_delay_max": 0,
                            "warmup_enabled": False})
        msgs = [self._queue(self.session, body=f"m{i}") for i in range(3)]
        with patch.object(type(self.session), "_gw", return_value={"wa_message_id": "C"}):
            self.env["whatsmeow.message"].cron_process_outgoing()
        self.assertEqual([m.state for m in msgs], ["sent"] * 3)

    def test_a_hand_sent_message_ignores_the_throttle(self):
        """Sending from the form is a human act at human speed; making the user
        wait would confuse without lowering the risk."""
        self.session.next_send_at = fields.Datetime.now() + timedelta(minutes=5)
        msg = self._queue(self.session)
        with patch.object(type(self.session), "_gw", return_value={"wa_message_id": "D"}):
            msg.action_send()
        self.assertEqual(msg.state, "sent")


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
class TestInboundFilterMatcher(WhatsmeowCommon):
    """The matcher is pure Python over a facts dict, so it needs no gateway and
    no stored message — just a rule and a dict."""

    GROUP_JID = "120363000000000000@g.us"

    def _rule(self, **vals):
        vals.setdefault("session_id", self.session.id)
        return self.env["whatsmeow.session.rule"].create(vals)

    def _facts(self, **over):
        facts = {
            "chat_type": "private", "message_type": "text",
            "partner_id": False, "chat_jid": "447700900123@s.whatsapp.net",
            "phone_tail": "7700900123", "sender_lid": "",
            "body": "hello there", "is_placeholder": False,
        }
        facts.update(over)
        return facts

    def test_empty_rule_matches_everything(self):
        rule = self._rule()
        self.assertTrue(rule._matches(self._facts()))
        self.assertTrue(rule._matches(self._facts(chat_type="group", body="")))

    def test_chat_type_criterion(self):
        rule = self._rule(chat_type="group")
        self.assertTrue(rule._matches(self._facts(chat_type="group")))
        self.assertFalse(rule._matches(self._facts(chat_type="private")))

    def test_message_type_criterion(self):
        rule = self._rule(message_type="image")
        self.assertTrue(rule._matches(self._facts(message_type="image")))
        self.assertFalse(rule._matches(self._facts(message_type="text")))

    def test_partner_criterion(self):
        alice = self.env["res.partner"].create({"name": "Alice"})
        bob = self.env["res.partner"].create({"name": "Bob"})
        rule = self._rule(partner_ids=[(6, 0, alice.ids)])
        self.assertTrue(rule._matches(self._facts(partner_id=alice.id)))
        self.assertFalse(rule._matches(self._facts(partner_id=bob.id)))
        self.assertFalse(rule._matches(self._facts(partner_id=False)))

    def test_chat_jids_criterion_multi_value(self):
        rule = self._rule(chat_jids=f"{self.GROUP_JID},\n999@g.us")
        self.assertTrue(rule._matches(self._facts(chat_jid=self.GROUP_JID)))
        self.assertTrue(rule._matches(self._facts(chat_jid="999@g.us")))
        self.assertFalse(rule._matches(self._facts(chat_jid="111@g.us")))

    def test_phones_criterion_last_ten_digits(self):
        """A rule keyed by a formatted number matches the bare tail the webhook
        computes, and vice versa — both reduce to the last 10 digits."""
        rule = self._rule(phones="+44 7700 900123, 966550199013")
        self.assertTrue(rule._matches(self._facts(phone_tail="7700900123")))
        self.assertTrue(rule._matches(self._facts(phone_tail="6550199013")))
        self.assertFalse(rule._matches(self._facts(phone_tail="0000000000")))
        # a LID-only sender has no phone tail, so a phone rule can't match it
        self.assertFalse(rule._matches(self._facts(phone_tail="")))

    def test_sender_lids_criterion(self):
        rule = self._rule(sender_lids="126864760766535")
        self.assertTrue(rule._matches(self._facts(sender_lid="126864760766535")))
        self.assertFalse(rule._matches(self._facts(sender_lid="999")))

    def test_keyword_is_case_insensitive_substring(self):
        rule = self._rule(keyword="STOP")
        self.assertTrue(rule._matches(self._facts(body="please stop now")))
        self.assertFalse(rule._matches(self._facts(body="carry on")))

    def test_keyword_never_matches_a_placeholder(self):
        """The placeholder's body is a stand-in, so a keyword can't be judged;
        the rule falls through and the real copy is judged later."""
        rule = self._rule(keyword="stop")
        self.assertFalse(rule._matches(self._facts(body="stop", is_placeholder=True)))
        self.assertTrue(rule._matches(self._facts(body="stop", is_placeholder=False)))

    def test_identity_rule_still_fires_on_a_placeholder(self):
        rule = self._rule(chat_jids=self.GROUP_JID)
        self.assertTrue(rule._matches(
            self._facts(chat_jid=self.GROUP_JID, is_placeholder=True)))

    def test_criteria_are_anded(self):
        """A rule with two criteria needs both to match."""
        rule = self._rule(chat_type="group", keyword="urgent")
        self.assertTrue(rule._matches(
            self._facts(chat_type="group", body="urgent: call me")))
        self.assertFalse(rule._matches(
            self._facts(chat_type="group", body="hi")))
        self.assertFalse(rule._matches(
            self._facts(chat_type="private", body="urgent")))


@tagged("post_install", "-at_install")
class TestInboundFilterEngine(WhatsmeowCommon):
    """`_inbound_decision` walks the rules in order and falls back to the
    session default."""

    def _facts(self, **over):
        facts = {
            "chat_type": "private", "message_type": "text", "partner_id": False,
            "chat_jid": "447700900123@s.whatsapp.net", "phone_tail": "7700900123",
            "sender_lid": "", "body": "hello", "is_placeholder": False,
        }
        facts.update(over)
        return facts

    def _rule(self, **vals):
        vals.setdefault("session_id", self.session.id)
        return self.env["whatsmeow.session.rule"].create(vals)

    def test_no_rules_uses_the_default(self):
        self.assertEqual(self.session.inbound_default, "accept")
        self.assertEqual(self.session._inbound_decision(self._facts()), "accept")
        self.session.inbound_default = "reject"
        self.assertEqual(self.session._inbound_decision(self._facts()), "reject")

    def test_first_match_wins(self):
        # lower sequence is evaluated first
        self._rule(sequence=20, keyword="hello", action="reject")
        self._rule(sequence=10, keyword="hello", action="accept")
        self.assertEqual(self.session._inbound_decision(self._facts()), "accept")

    def test_blocklist_accept_default_with_reject_rule(self):
        self.session.inbound_default = "accept"
        self._rule(chat_type="group", action="reject")
        self.assertEqual(
            self.session._inbound_decision(self._facts(chat_type="group")), "reject")
        self.assertEqual(
            self.session._inbound_decision(self._facts(chat_type="private")), "accept")

    def test_allowlist_reject_default_with_accept_rule(self):
        self.session.inbound_default = "reject"
        self._rule(chat_jids="120363@g.us", action="accept")
        self.assertEqual(
            self.session._inbound_decision(self._facts(chat_jid="120363@g.us")), "accept")
        self.assertEqual(
            self.session._inbound_decision(self._facts(chat_jid="other@g.us")), "reject")

    def test_accept_a_group_except_one_member(self):
        """The mixed case the single-default+rules design exists for."""
        self.session.inbound_default = "reject"
        # reject the one noisy member first, then accept the whole group
        self._rule(sequence=10, phones="447700900999", action="reject")
        self._rule(sequence=20, chat_jids="site@g.us", action="accept")
        group = {"chat_type": "group", "chat_jid": "site@g.us"}
        self.assertEqual(self.session._inbound_decision(
            self._facts(phone_tail="7700900999", **group)), "reject")
        self.assertEqual(self.session._inbound_decision(
            self._facts(phone_tail="7700900123", **group)), "accept")

    def test_empty_catch_all_rule(self):
        self._rule(sequence=99, action="reject")  # all-empty -> matches anything
        self.assertEqual(self.session._inbound_decision(self._facts()), "reject")

    def test_archived_rules_are_skipped(self):
        rule = self._rule(keyword="hello", action="reject")
        self.assertEqual(self.session._inbound_decision(self._facts()), "reject")
        rule.active = False
        self.session.invalidate_recordset(["inbound_rule_ids"])
        self.assertEqual(self.session._inbound_decision(self._facts()), "accept")


@tagged("post_install", "-at_install")
class TestAutoMarkRead(WhatsmeowCommon):
    """Read receipts go out on accept, once, and only when opted in."""

    def setUp(self):
        super().setUp()
        from odoo.addons.whatsmeow.controllers.webhook import WhatsmeowWebhook
        self.ctrl = WhatsmeowWebhook()

    def _read_calls(self, gw):
        return [c for c in gw.call_args_list if c.args[1].endswith("/read")]

    def test_off_by_default_sends_no_receipt(self):
        with patch.object(type(self.session), "_gw", return_value={}) as gw:
            self.ctrl._on_message(self.env, self.session, {
                "wa_message_id": "MR-OFF", "sender_phone": "447700900123",
                "body": "hi",
            })
        self.assertFalse(self._read_calls(gw))

    def test_accepted_message_is_marked_read(self):
        self.session.auto_mark_read = True
        with patch.object(type(self.session), "_gw", return_value={}) as gw:
            self.ctrl._on_message(self.env, self.session, {
                "wa_message_id": "MR-ON", "sender_phone": "447700900123",
                "body": "hi", "chat_jid": "447700900123@s.whatsapp.net",
            })
        calls = self._read_calls(gw)
        self.assertEqual(len(calls), 1)
        payload = calls[0].args[2]
        self.assertEqual(payload["message_ids"], ["MR-ON"])
        # A 1:1 receipt names no author: the chat is the sender.
        self.assertEqual(payload["sender"], "")

    def test_a_group_receipt_names_the_participant(self):
        self.session.auto_mark_read = True
        with patch.object(type(self.session), "_gw", return_value={}) as gw:
            self.ctrl._on_message(self.env, self.session, {
                "wa_message_id": "MR-GRP", "sender_phone": "447700900123",
                "body": "hi all", "chat_jid": "120363@g.us",
                "sender_jid": "447700900123@s.whatsapp.net",
            })
        payload = self._read_calls(gw)[0].args[2]
        self.assertEqual(payload["sender"], "447700900123@s.whatsapp.net")
        self.assertEqual(payload["jid"], "120363@g.us")

    def test_a_rejected_message_is_never_marked_read(self):
        """Blue-ticking something nobody will ever see is the one outcome this
        must not produce."""
        self.session.auto_mark_read = True
        self.session.inbound_default = "reject"
        with patch.object(type(self.session), "_gw", return_value={}) as gw, \
                mute_logger("odoo.addons.whatsmeow.controllers.webhook"):
            self.ctrl._on_message(self.env, self.session, {
                "wa_message_id": "MR-REJ", "sender_phone": "447700900123",
                "body": "spam",
            })
        self.assertFalse(self._read_calls(gw))

    def test_a_retry_does_not_tick_twice(self):
        self.session.auto_mark_read = True
        data = {"wa_message_id": "MR-RETRY", "sender_phone": "447700900123",
                "body": "hi"}
        with patch.object(type(self.session), "_gw", return_value={}) as gw:
            self.ctrl._on_message(self.env, self.session, data)
            self.ctrl._on_message(self.env, self.session, data)
        self.assertEqual(len(self._read_calls(gw)), 1)

    def test_a_gateway_failure_does_not_break_the_webhook(self):
        self.session.auto_mark_read = True
        with patch.object(type(self.session), "_gw",
                          side_effect=UserError("gateway down")), \
                self.assertLogs("odoo.addons.whatsmeow.models.whatsmeow_message",
                                level="WARNING"):
            self.ctrl._on_message(self.env, self.session, {
                "wa_message_id": "MR-FAIL", "sender_phone": "447700900123",
                "body": "hi",
            })
        # The message is still stored; only the courtesy receipt was lost.
        self.assertTrue(self.env["whatsmeow.message"].search(
            [("wa_message_id", "=", "MR-FAIL")]))


@tagged("post_install", "-at_install")
class TestInboundFilterWebhook(WhatsmeowCommon):
    """End-to-end: the filter decides whether a webhook creates a record."""

    def setUp(self):
        super().setUp()
        from odoo.addons.whatsmeow.controllers.webhook import WhatsmeowWebhook
        self.ctrl = WhatsmeowWebhook()

    def _messages(self, wa_id):
        return self.env["whatsmeow.message"].search([("wa_message_id", "=", wa_id)])

    def test_no_rules_stores_inbound_exactly_as_before(self):
        """Backward compatibility: a fresh session accepts everything."""
        self.ctrl._on_message(self.env, self.session, {
            "wa_message_id": "BC-1", "sender_phone": "447700900123", "body": "hi",
        })
        self.assertEqual(len(self._messages("BC-1")), 1)

    def test_rejected_message_creates_no_record_and_posts_nothing(self):
        partner = self.env["res.partner"].create({
            "name": "Blocked", "phone": "+44 7700 900123",
        })
        self.session.inbound_default = "reject"
        with self.assertLogs(
                "odoo.addons.whatsmeow.controllers.webhook", level="INFO"):
            self.ctrl._on_message(self.env, self.session, {
                "wa_message_id": "REJ-1", "sender_phone": "447700900123",
                "body": "go away",
            })
        self.assertFalse(self._messages("REJ-1"))
        # No chatter post carries the rejected body (res.partner may auto-log a
        # "created" note, so assert on the content rather than a bare count).
        self.assertFalse(self.env["mail.message"].search([
            ("model", "=", "res.partner"), ("res_id", "=", partner.id),
            ("body", "like", "go away"),
        ]))

    def test_accepted_message_behaves_normally(self):
        self.session.inbound_default = "reject"
        self.env["whatsmeow.session.rule"].create({
            "session_id": self.session.id, "phones": "447700900123",
            "action": "accept",
        })
        self.ctrl._on_message(self.env, self.session, {
            "wa_message_id": "ACC-1", "sender_phone": "447700900123", "body": "hi",
        })
        self.assertEqual(len(self._messages("ACC-1")), 1)

    def test_a_rejected_retry_is_still_a_no_op(self):
        self.session.inbound_default = "reject"
        data = {"wa_message_id": "REJ-RETRY", "sender_phone": "447700900123",
                "body": "spam"}
        with mute_logger("odoo.addons.whatsmeow.controllers.webhook"):
            self.ctrl._on_message(self.env, self.session, data)
            self.ctrl._on_message(self.env, self.session, data)
        self.assertFalse(self._messages("REJ-RETRY"))

    def test_keyword_only_rule_lets_a_placeholder_through_then_judges_the_real_copy(self):
        """A keyword-reject rule can't judge the empty first copy, so the
        placeholder is accepted (stored); the real copy is a duplicate and
        merges, keeping today's dedup behaviour intact."""
        self.session.inbound_default = "accept"
        self.env["whatsmeow.session.rule"].create({
            "session_id": self.session.id, "keyword": "stop", "action": "reject",
        })
        # placeholder: keyword can't be judged -> falls through -> default accept
        self.ctrl._on_message(self.env, self.session, {
            "wa_message_id": "PH-KW", "sender_phone": "447700900123",
            "body": "[unsupported message type: text]", "placeholder": True,
        })
        self.assertEqual(len(self._messages("PH-KW")), 1)

    def test_identity_rule_drops_a_placeholder_immediately(self):
        self.session.inbound_default = "accept"
        self.env["whatsmeow.session.rule"].create({
            "session_id": self.session.id, "phones": "447700900123",
            "action": "reject",
        })
        with mute_logger("odoo.addons.whatsmeow.controllers.webhook"):
            self.ctrl._on_message(self.env, self.session, {
                "wa_message_id": "PH-ID", "sender_phone": "447700900123",
                "body": "[unsupported message type: text]", "placeholder": True,
            })
        self.assertFalse(self._messages("PH-ID"))

    def test_allowlist_by_partner_drops_a_lid_only_sender(self):
        """Correct but surprising: a LID-only sender resolves to no partner, so
        an allowlist keyed on partners rejects it."""
        alice = self.env["res.partner"].create({"name": "Alice"})
        self.session.inbound_default = "reject"
        self.env["whatsmeow.session.rule"].create({
            "session_id": self.session.id, "partner_ids": [(6, 0, alice.ids)],
            "action": "accept",
        })
        with mute_logger("odoo.addons.whatsmeow.controllers.webhook"):
            self.ctrl._on_message(self.env, self.session, {
                "wa_message_id": "LID-DROP", "sender_phone": "",
                "sender_lid": "126864760766535", "body": "hello",
            })
        self.assertFalse(self._messages("LID-DROP"))

    def test_sender_lids_rule_admits_a_lid_only_sender(self):
        self.session.inbound_default = "reject"
        self.env["whatsmeow.session.rule"].create({
            "session_id": self.session.id, "sender_lids": "126864760766535",
            "action": "accept",
        })
        self.ctrl._on_message(self.env, self.session, {
            "wa_message_id": "LID-ADMIT", "sender_phone": "",
            "sender_lid": "126864760766535", "body": "hello",
        })
        msg = self._messages("LID-ADMIT")
        self.assertEqual(len(msg), 1)
        self.assertEqual(msg.sender_lid, "126864760766535")


@tagged("post_install", "-at_install")
class TestWebhookRouting(WhatsmeowCommon):

    def test_lid_only_sender_is_not_stored_as_a_phone(self):
        """Regression: the gateway used to send Sender.User, which for LID
        addressing is a random 15-digit id, not a phone number. It landed in
        `phone` and looked like a real number."""
        from odoo.addons.whatsmeow.controllers.webhook import WhatsmeowWebhook
        WhatsmeowWebhook()._on_message(self.env, self.session, {
            "wa_message_id": "LID-ONLY-1",
            "sender_phone": "",                     # gateway could not resolve one
            "sender_lid": "126864760766535",
            "addressing_mode": "lid",
            "body": "hello",
        })
        msg = self.env["whatsmeow.message"].search([("wa_message_id", "=", "LID-ONLY-1")])
        self.assertEqual(len(msg), 1)
        self.assertFalse(msg.phone, "a LID must never be stored as a phone number")
        self.assertEqual(msg.sender_lid, "126864760766535")
        self.assertFalse(msg.partner_id)

    def test_resolved_phone_still_matches_partner(self):
        """When the gateway does resolve the LID to a phone, matching works.
        Uses a reserved-range number so a real contact in a dev database can't
        collide with it."""
        from odoo.addons.whatsmeow.controllers.webhook import WhatsmeowWebhook
        partner = self.env["res.partner"].create({
            "name": "LID Test Contact", "phone": "+966 55 019 9012",
        })
        WhatsmeowWebhook()._on_message(self.env, self.session, {
            "wa_message_id": "LID-RESOLVED-1",
            "sender_phone": "966550199012",
            "sender_lid": "126864760766535",
            "addressing_mode": "lid",
            "body": "hello",
        })
        msg = self.env["whatsmeow.message"].search([("wa_message_id", "=", "LID-RESOLVED-1")])
        self.assertEqual(msg.phone, "966550199012")
        self.assertEqual(msg.sender_lid, "126864760766535")
        self.assertEqual(msg.partner_id, partner)

    def test_inbound_media_is_queued_not_downloaded_inline(self):
        """The webhook must stay fast: it records metadata and lets the cron
        fetch the bytes, otherwise a big file stalls it past the gateway's
        timeout and the gateway just retries."""
        from odoo.addons.whatsmeow.controllers.webhook import WhatsmeowWebhook
        WhatsmeowWebhook()._on_message(self.env, self.session, {
            "wa_message_id": "IN-VOICE-1",
            "sender_phone": "447700900123",
            "body": "",
            "media": {
                "kind": "audio", "mimetype": "audio/ogg; codecs=opus",
                "filename": "audio_IN-VOICE-1.ogg", "size": 4096,
                "seconds": 7, "ptt": True,
            },
        })
        msg = self.env["whatsmeow.message"].search([("wa_message_id", "=", "IN-VOICE-1")])
        self.assertEqual(len(msg), 1)
        self.assertEqual(msg.message_type, "audio")
        self.assertEqual(msg.media_state, "pending")
        self.assertTrue(msg.is_voice_note)
        self.assertEqual(msg.media_duration, 7)
        self.assertEqual(msg.media_filename, "audio_IN-VOICE-1.ogg")
        self.assertFalse(msg.media_data, "the webhook must not fetch the bytes itself")

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


@tagged("post_install", "-at_install")
class TestOptOut(WhatsmeowCommon):
    """Opt-out is the one gate no path may route around (PLAN.md §12.3).

    Unlike the volume caps it applies to interactive sends too: a contact who
    asked to be left alone did not ask only the queue.
    """

    def setUp(self):
        super().setUp()
        self.partner = self.env["res.partner"].create({
            "name": "Alice", "phone": "+44 7700 900123",
        })

    def _queue(self, **over):
        vals = {
            "session_id": self.session.id, "phone": "447700900123",
            "direction": "out", "body": "hi",
        }
        vals.update(over)
        return self.env["whatsmeow.message"].create(vals)

    def test_optout_blocks_a_hand_sent_message(self):
        self.partner.whatsmeow_optout = True
        msg = self._queue(partner_id=self.partner.id)
        with patch.object(type(self.session), "_gw") as gw:
            with self.assertRaises(UserError):
                msg.action_send()
        self.assertEqual(gw.call_count, 0, "nothing may reach the gateway")

    def test_optout_blocks_the_queue_without_raising(self):
        """The cron has nobody to tell, so it marks the row and carries on."""
        self.partner.whatsmeow_optout = True
        blocked = self._queue(partner_id=self.partner.id)
        allowed = self._queue(phone="447700900999", body="ok")
        with patch.object(type(self.session), "_gw",
                          return_value={"wa_message_id": "OK"}) as gw:
            self.env["whatsmeow.message"].cron_process_outgoing()
        self.assertEqual(blocked.state, "error")
        self.assertIn("opted out", blocked.error_message)
        self.assertEqual(allowed.state, "sent")
        self.assertEqual(gw.call_count, 1, "only the allowed message was sent")

    def test_optout_is_matched_by_phone_without_a_partner_link(self):
        """A message typed against a bare number must not slip past."""
        self.partner.whatsmeow_optout = True
        msg = self._queue()
        self.assertFalse(msg.partner_id)
        with patch.object(type(self.session), "_gw") as gw:
            with self.assertRaises(UserError):
                msg.action_send()
        self.assertEqual(gw.call_count, 0)

    def test_a_blocked_batch_does_not_roll_back_a_sent_one(self):
        """The check runs over the whole set before anything is sent, so the
        raise cannot undo a message WhatsApp already has."""
        self.partner.whatsmeow_optout = True
        blocked = self._queue(partner_id=self.partner.id)
        other = self._queue(phone="447700900999")
        with patch.object(type(self.session), "_gw",
                          return_value={"wa_message_id": "Z"}) as gw:
            with self.assertRaises(UserError):
                (blocked | other).action_send()
        self.assertEqual(gw.call_count, 0)
        self.assertEqual(other.state, "outgoing")

    def test_a_group_message_is_not_blocked_by_one_member(self):
        """An individual's opt-out cannot speak for a whole group chat."""
        self.partner.whatsmeow_optout = True
        msg = self._queue(phone="", chat_jid="site@g.us",
                          partner_id=self.partner.id)
        with patch.object(type(self.session), "_gw",
                          return_value={"wa_message_id": "G"}):
            msg.action_send()
        self.assertEqual(msg.state, "sent")

    def test_optout_is_not_lifted_by_a_second_stop(self):
        self.partner._whatsmeow_optout("first")
        first = self.partner.whatsmeow_optout_date
        self.partner._whatsmeow_optout("second")
        self.assertEqual(self.partner.whatsmeow_optout_date, first)
        self.assertEqual(self.partner.whatsmeow_optout_reason, "first")


@tagged("post_install", "-at_install")
class TestKeywordOptOut(WhatsmeowCommon):
    """"STOP" arriving inbound flags the contact, via a rule rather than a
    hard-coded word list."""

    def setUp(self):
        super().setUp()
        from odoo.addons.whatsmeow.controllers.webhook import WhatsmeowWebhook
        self.ctrl = WhatsmeowWebhook()
        self.partner = self.env["res.partner"].create({
            "name": "Alice", "phone": "+44 7700 900123",
        })
        self.rule = self.env["whatsmeow.session.rule"].create({
            "session_id": self.session.id, "sequence": 1,
            "name": "Stop keyword", "action": "optout", "keyword": "stop",
        })

    def _inbound(self, **over):
        data = {
            "wa_message_id": "IN1", "sender_phone": "447700900123", "body": "stop",
        }
        data.update(over)
        self.ctrl._on_message(self.env, self.session, data)

    def test_keyword_sets_the_flag(self):
        self._inbound()
        self.assertTrue(self.partner.whatsmeow_optout)
        self.assertTrue(self.partner.whatsmeow_optout_date)

    def test_optout_rule_does_not_decide_the_disposition(self):
        """It flags the sender and steps aside: the request itself is kept."""
        self._inbound()
        msg = self.env["whatsmeow.message"].search([("wa_message_id", "=", "IN1")])
        self.assertEqual(len(msg), 1)
        self.assertEqual(self.session._inbound_decision({
            "chat_type": "private", "message_type": "text",
            "partner_id": self.partner.id, "sender_state": "existing",
            "chat_jid": "", "phone_tail": "7700900123", "sender_lid": "",
            "body": "stop", "is_placeholder": False,
        }), "accept")

    def test_a_rejected_message_still_opts_out(self):
        """Someone asking to be left alone is exactly whose wish to record."""
        self.session.inbound_default = "reject"
        self._inbound()
        self.assertTrue(self.partner.whatsmeow_optout)
        self.assertFalse(
            self.env["whatsmeow.message"].search([("wa_message_id", "=", "IN1")]))

    def test_the_placeholder_does_not_opt_out_but_the_real_copy_does(self):
        """A keyword cannot be judged on WhatsApp's empty first copy."""
        self._inbound(body="", placeholder=True)
        self.assertFalse(self.partner.whatsmeow_optout)
        self._inbound(body="STOP")            # the real copy of the same id
        self.assertTrue(self.partner.whatsmeow_optout)

    def test_an_unrelated_message_does_not_opt_out(self):
        self._inbound(body="hello there")
        self.assertFalse(self.partner.whatsmeow_optout)

    def test_a_lid_only_sender_cannot_be_flagged(self):
        """There is no contact to put the flag on; it must not crash."""
        self._inbound(sender_phone="", sender_lid="12345@lid")
        self.assertFalse(self.partner.whatsmeow_optout)


@tagged("post_install", "-at_install")
class TestWarmupCaps(WhatsmeowCommon):
    """The ramp bounds the day; the pacing only spaces sends apart (§12.2)."""

    def setUp(self):
        super().setUp()
        self.session.write({
            "send_delay_min": 0, "send_delay_max": 0,
            "warmup_enabled": True, "warmup_start_date": fields.Date.today(),
            "daily_cap_base": 20, "daily_cap_growth": 1.3, "daily_cap_max": 500,
        })

    def _queue(self, n=1):
        return self.env["whatsmeow.message"].create([{
            "session_id": self.session.id, "phone": "447700900123",
            "direction": "out", "body": f"m{i}",
        } for i in range(n)])

    def test_the_ramp_curve(self):
        today = self.session._local_today()
        for days, expected in ((0, 20), (1, 26), (7, 126), (30, 500), (365, 500)):
            self.session.warmup_start_date = today - timedelta(days=days)
            self.assertEqual(self.session._daily_cap(), expected,
                             f"wrong allowance {days} days in")

    def test_a_future_start_date_gives_day_one(self):
        self.session.warmup_start_date = self.session._local_today() + timedelta(days=5)
        self.assertEqual(self.session._daily_cap(), 20)

    def test_disabled_warmup_is_unlimited(self):
        self.session.warmup_enabled = False
        self.assertEqual(self.session._daily_cap(), 0)
        self.assertEqual(self.session._seconds_until_sendable(), 0.0)

    def test_growth_cannot_shrink_the_allowance(self):
        with self.assertRaises(ValidationError):
            self.session.daily_cap_growth = 0.9

    def test_the_cron_stops_at_the_cap(self):
        self.session.write({"daily_cap_base": 3, "hourly_cap": 99})
        msgs = self._queue(5)
        with patch.object(type(self.session), "_gw",
                          return_value={"wa_message_id": "A"}) as gw:
            self.env["whatsmeow.message"].cron_process_outgoing()
        self.assertEqual(gw.call_count, 3)
        self.assertEqual([m.state for m in msgs],
                         ["sent"] * 3 + ["outgoing"] * 2)

        # A second run the same day adds nothing, and touches no gateway.
        with patch.object(type(self.session), "_gw") as gw:
            self.env["whatsmeow.message"].cron_process_outgoing()
        self.assertEqual(gw.call_count, 0)

    def test_the_remainder_drains_the_next_day(self):
        self.session.write({"daily_cap_base": 2, "hourly_cap": 99})
        msgs = self._queue(4)
        with patch.object(type(self.session), "_gw",
                          return_value={"wa_message_id": "A"}):
            self.env["whatsmeow.message"].cron_process_outgoing()
        self.assertEqual(len(msgs.filtered(lambda m: m.state == "sent")), 2)

        # Move yesterday's sends back rather than the clock: same effect on the
        # count, and the ramp gives day two a larger allowance anyway.
        msgs.filtered(lambda m: m.sent_date).sent_date = \
            fields.Datetime.now() - timedelta(days=1)
        self.session.warmup_start_date = self.session._local_today() - timedelta(days=1)
        with patch.object(type(self.session), "_gw",
                          return_value={"wa_message_id": "B"}):
            self.env["whatsmeow.message"].cron_process_outgoing()
        self.assertEqual([m.state for m in msgs], ["sent"] * 4)

    def test_the_hourly_cap_holds_a_days_worth_back(self):
        """A daily allowance must not leave in one burst."""
        self.session.write({"daily_cap_base": 24, "hourly_cap": 0})
        self.assertEqual(self.session._hourly_cap(), 2)
        msgs = self._queue(4)
        with patch.object(type(self.session), "_gw",
                          return_value={"wa_message_id": "A"}) as gw:
            self.env["whatsmeow.message"].cron_process_outgoing()
        self.assertEqual(gw.call_count, 2)
        self.assertEqual(len(msgs.filtered(lambda m: m.state == "outgoing")), 2)

    def test_a_capped_session_does_not_stall_another(self):
        self.session.write({"daily_cap_base": 1, "hourly_cap": 99})
        other = self.env["whatsmeow.session"].create({
            "name": "Client Beta", "code": "client_beta",
            "connection_id": self.connection.id,
            "send_delay_min": 0, "send_delay_max": 0,
        })
        mine = self._queue(2)
        theirs = self.env["whatsmeow.message"].create({
            "session_id": other.id, "phone": "447700900999",
            "direction": "out", "body": "hi",
        })
        with patch.object(type(self.session), "_gw",
                          return_value={"wa_message_id": "A"}):
            self.env["whatsmeow.message"].cron_process_outgoing()
        self.assertEqual(theirs.state, "sent")
        self.assertEqual(len(mine.filtered(lambda m: m.state == "outgoing")), 1)

    def test_a_hand_sent_message_ignores_the_cap(self):
        """A reply to someone who wrote first is the safest traffic there is;
        only queued/bulk traffic is budgeted."""
        self.session.daily_cap_base = 1
        first, second = self._queue(2)
        with patch.object(type(self.session), "_gw",
                          return_value={"wa_message_id": "A"}):
            first.action_send()
            second.action_send()
        self.assertEqual([first.state, second.state], ["sent", "sent"])

    def test_the_day_rolls_over_in_the_sessions_timezone(self):
        """A cap that resets at UTC noon is wrong twice a day for a Gulf client."""
        self.session.tz = "Asia/Riyadh"          # UTC+3, no DST
        midnight = self.session._local_midnight_utc()
        self.assertEqual((midnight.hour, midnight.minute), (21, 0))
        self.assertEqual(
            self.session._local_today(),
            (fields.Datetime.now() + timedelta(hours=3)).date())

    def test_the_first_send_starts_the_ramp(self):
        self.session.warmup_start_date = False
        msg = self._queue()
        with patch.object(type(self.session), "_gw",
                          return_value={"wa_message_id": "A"}):
            msg.action_send()
        self.assertEqual(self.session.warmup_start_date, self.session._local_today())


@tagged("post_install", "-at_install")
class TestRecipientValidation(WhatsmeowCommon):
    """Sending into the void is a bulk-sender fingerprint, and it is entirely
    preventable — the gateway can ask first (PLAN.md §12.4)."""

    def setUp(self):
        super().setUp()
        self.session.write({"send_delay_min": 0, "send_delay_max": 0,
                            "warmup_enabled": False})
        self.alice = self.env["res.partner"].create({
            "name": "Alice", "phone": "+44 7700 900123",
        })
        self.bob = self.env["res.partner"].create({
            "name": "Bob", "phone": "+44 7700 900456",
        })

    def _queue(self, partner, phone=None):
        digits = re.sub(r"\D", "", partner.phone or "")
        return self.env["whatsmeow.message"].create({
            "session_id": self.session.id, "partner_id": partner.id,
            "phone": phone if phone is not None else digits,
            "direction": "out", "body": "hi",
        })

    def test_check_numbers_chunks_and_reads_the_answers(self):
        with patch.object(type(self.session), "_gw", return_value={"results": [
            {"number": "447700900123", "registered": True,
             "jid": "447700900123@s.whatsapp.net"},
            {"number": "447700900456", "registered": False, "jid": ""},
        ]}) as gw:
            answers = self.session._check_numbers(
                ["+44 7700 900123", "447700900456"])
        self.assertEqual(answers, {"447700900123": True, "447700900456": False})
        self.assertIn("/check", gw.call_args.args[1])
        self.assertEqual(gw.call_args.args[2]["phones"],
                         ["447700900123", "447700900456"])

    def test_a_throttled_gateway_stops_the_batch(self):
        """The gateway drips the lookups on purpose; asking harder defeats it."""
        with patch.object(type(self.session), "_gw", return_value={
                "results": [], "throttled": True}) as gw:
            self.session._check_numbers([str(700000000 + i) for i in range(120)])
        self.assertEqual(gw.call_count, 1, "stop after the first throttled batch")

    def test_an_unanswered_number_is_not_a_no(self):
        """Silence is an open question. Recording it as 'not on WhatsApp' would
        permanently stop us messaging a real contact."""
        with patch.object(type(self.session), "_gw", return_value={"results": [
            {"number": "447700900123", "registered": True, "jid": "x"},
        ]}):
            answers = self.session._check_numbers(["447700900123", "447700900456"])
        self.assertNotIn("447700900456", answers)

    def test_the_cron_validates_the_numbers_a_batch_is_queued_to(self):
        self._queue(self.alice)
        self._queue(self.bob)
        with patch.object(type(self.session), "_gw", return_value={"results": [
            {"number": "447700900123", "registered": True, "jid": "x"},
            {"number": "447700900456", "registered": False, "jid": ""},
        ]}) as gw:
            self.env["whatsmeow.message"].cron_check_numbers()
        self.assertEqual(gw.call_count, 1)
        self.assertEqual(self.alice.whatsmeow_registered, "yes")
        self.assertEqual(self.bob.whatsmeow_registered, "no")
        self.assertTrue(self.alice.whatsmeow_registered_date)

    def test_the_cron_asks_nothing_about_a_recently_checked_number(self):
        self.alice.write({"whatsmeow_registered": "yes",
                          "whatsmeow_registered_date": fields.Datetime.now()})
        self._queue(self.alice)
        with patch.object(type(self.session), "_gw") as gw:
            self.env["whatsmeow.message"].cron_check_numbers()
        self.assertEqual(gw.call_count, 0)

    def test_a_stale_answer_is_asked_again(self):
        self.alice.write({
            "whatsmeow_registered": "no",
            "whatsmeow_registered_date": fields.Datetime.now() - timedelta(days=40),
        })
        self._queue(self.alice)
        with patch.object(type(self.session), "_gw", return_value={"results": [
            {"number": "447700900123", "registered": True, "jid": "x"},
        ]}) as gw:
            self.env["whatsmeow.message"].cron_check_numbers()
        self.assertEqual(gw.call_count, 1)
        self.assertEqual(self.alice.whatsmeow_registered, "yes")

    def test_the_queue_skips_a_number_known_to_be_dead(self):
        self.bob.write({"whatsmeow_registered": "no",
                        "whatsmeow_registered_date": fields.Datetime.now()})
        dead = self._queue(self.bob)
        alive = self._queue(self.alice)
        with patch.object(type(self.session), "_gw",
                          return_value={"wa_message_id": "A"}) as gw, \
                mute_logger("odoo.addons.whatsmeow.models.whatsmeow_message"):
            self.env["whatsmeow.message"].cron_process_outgoing()
        self.assertEqual(gw.call_count, 1, "the dead number burns no send")
        self.assertEqual(dead.state, "error")
        self.assertIn("not on WhatsApp", dead.error_message)
        self.assertEqual(alive.state, "sent")

    def test_a_stale_no_does_not_block_a_send(self):
        """Past the TTL the gateway would ask WhatsApp again, so Odoo must not
        treat the old answer as final."""
        self.bob.write({
            "whatsmeow_registered": "no",
            "whatsmeow_registered_date": fields.Datetime.now() - timedelta(days=40),
        })
        msg = self._queue(self.bob)
        with patch.object(type(self.session), "_gw",
                          return_value={"wa_message_id": "A"}):
            msg.action_send()
        self.assertEqual(msg.state, "sent")

    def test_changing_the_number_forgets_the_answer(self):
        """The old answer was about the old number."""
        self.alice.write({"whatsmeow_registered": "yes",
                          "whatsmeow_registered_date": fields.Datetime.now()})
        self.alice.write({"phone": "+44 7700 900999"})
        self.assertFalse(self.alice.whatsmeow_registered)
        self.assertFalse(self.alice.whatsmeow_registered_date)

    def test_rewriting_the_same_number_keeps_the_answer(self):
        self.alice.write({"whatsmeow_registered": "yes",
                          "whatsmeow_registered_date": fields.Datetime.now()})
        self.alice.write({"phone": self.alice.phone, "comment": "touched"})
        self.assertEqual(self.alice.whatsmeow_registered, "yes")

    def test_an_opted_out_contact_is_never_checked(self):
        """No point spending a lookup on someone we may not message anyway."""
        self.alice.whatsmeow_optout = True
        self._queue(self.alice)
        with patch.object(type(self.session), "_gw") as gw:
            self.env["whatsmeow.message"].cron_check_numbers()
        self.assertEqual(gw.call_count, 0)

    def test_a_failing_gateway_leaves_the_answers_alone(self):
        self._queue(self.alice)
        with patch.object(type(self.session), "_gw", side_effect=UserError("down")), \
                mute_logger("odoo.addons.whatsmeow.models.whatsmeow_message"):
            self.env["whatsmeow.message"].cron_check_numbers()
        self.assertFalse(self.alice.whatsmeow_registered)


@tagged("post_install", "-at_install")
class TestMarkupRendering(TransactionCase):
    """WhatsApp's markers, as the chatter draws them.

    The same grammar the composer's preview renders in JavaScript
    (`whatsmeow_template/static/tests/whatsmeow_markup.test.js`): an author
    formats a message, checks it in the preview, and must find the same thing
    in the log. The cases below are that file's, so a change on one side that
    is not made on the other fails here.
    """

    def _render(self, text):
        from odoo.addons.whatsmeow.models.whatsmeow_markup import render_markup
        return str(render_markup(text))

    def test_renders_the_four_marks(self):
        self.assertEqual(self._render("*a*"), "<strong>a</strong>")
        self.assertEqual(self._render("_a_"), "<em>a</em>")
        self.assertEqual(self._render("~a~"), "<s>a</s>")
        self.assertEqual(self._render("```a```"), "<code>a</code>")

    def test_marks_nest(self):
        self.assertEqual(self._render("*bold _and italic_*"),
                         "<strong>bold <em>and italic</em></strong>")

    def test_leaves_markers_a_phone_would_not_render(self):
        # Whitespace inside the marker, and a marker spanning a line break:
        # neither renders on a phone, so neither may render here.
        self.assertEqual(self._render("*a *"), "*a *")
        self.assertEqual(self._render("*a\nb*"), "*a<br/>b*")

    def test_monospace_spans_lines_and_suppresses_other_marks(self):
        self.assertEqual(self._render("```*a*\nb```"), "<code>*a*<br/>b</code>")

    def test_unterminated_fence_stays_literal(self):
        self.assertEqual(self._render("```a"), "```a")

    def test_escapes_the_body_so_it_can_never_be_markup(self):
        """Inbound text is untrusted and lands in a chatter entry."""
        self.assertEqual(self._render("<b>&</b>"), "&lt;b&gt;&amp;&lt;/b&gt;")
        self.assertNotIn("<script", self._render("<script>alert(1)</script>"))

    def test_empty_body(self):
        self.assertEqual(self._render(""), "")
        self.assertEqual(self._render(False), "")

    def test_a_real_message(self):
        self.assertEqual(
            self._render("Hello _ACME Ltd_,\n\n*Quotation S05504* is ready."),
            "Hello <em>ACME Ltd</em>,<br/><br/>"
            "<strong>Quotation S05504</strong> is ready.",
        )
