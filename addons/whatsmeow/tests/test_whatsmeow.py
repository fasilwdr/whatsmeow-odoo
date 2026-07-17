import base64
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
        self.session.write({"send_delay_min": 0, "send_delay_max": 0})
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
