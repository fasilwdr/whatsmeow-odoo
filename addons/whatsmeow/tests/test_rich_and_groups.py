from unittest.mock import patch

from psycopg2 import IntegrityError

from odoo.exceptions import ValidationError
from odoo.tests import Form, tagged
from odoo.tools import mute_logger

from ..controllers.webhook import WhatsmeowWebhook
from ..models.whatsmeow_markup import render_markup
from ..models.whatsmeow_message import (
    MEDIA_MESSAGE_TYPES, MESSAGE_TYPES, RICH_MESSAGE_TYPES,
)
from .test_whatsmeow import WhatsmeowCommon

GROUP_JID = "120363000000000000@g.us"


@tagged("post_install", "-at_install")
class TestRichInbound(WhatsmeowCommon):
    """A location, a poll, an event and a contact card have no body of their
    own. They used to arrive as "[unsupported message type: ...]"; the gateway
    now renders each into the body and names its kind."""

    def setUp(self):
        super().setUp()
        self.ctrl = WhatsmeowWebhook()

    def _receive(self, wa_id, **data):
        self.ctrl._on_message(self.env, self.session, {
            "wa_message_id": wa_id, "sender_phone": "447700900123", **data,
        })
        return self.env["whatsmeow.message"].search([("wa_message_id", "=", wa_id)])

    def test_type_sets_partition_the_selection(self):
        """Every kind is text, a file or a rendered one — a kind added to the
        selection and forgotten here would fall through every view's test."""
        kinds = {kind for kind, _label in MESSAGE_TYPES}
        self.assertEqual(
            kinds, {"text"} | set(MEDIA_MESSAGE_TYPES) | set(RICH_MESSAGE_TYPES))
        self.assertFalse(set(MEDIA_MESSAGE_TYPES) & set(RICH_MESSAGE_TYPES))

    def test_location_is_typed_and_keeps_its_coordinates(self):
        msg = self._receive(
            "LOC1", kind="location",
            body="📍 Kingdom Centre\nhttps://maps.google.com/?q=24.713552,46.675297",
            location={"latitude": 24.713552, "longitude": 46.675297,
                      "name": "Kingdom Centre"},
        )
        self.assertEqual(msg.message_type, "location")
        self.assertAlmostEqual(msg.latitude, 24.713552, places=6)
        self.assertAlmostEqual(msg.longitude, 46.675297, places=6)
        self.assertFalse(msg.is_placeholder)
        self.assertEqual(msg.media_state, "none")
        self.assertIn("Kingdom Centre", msg.body)

    def test_poll_event_and_contact_are_typed(self):
        for kind in ("poll", "event", "contact"):
            msg = self._receive(f"RICH-{kind}", kind=kind, body=f"a {kind}")
            self.assertEqual(msg.message_type, kind)
            self.assertEqual(msg.body, f"a {kind}")

    def test_event_with_a_pinned_venue_keeps_the_pin(self):
        msg = self._receive(
            "EVT1", kind="event", body="📅 Dinner",
            location={"latitude": 24.7, "longitude": 46.7})
        self.assertEqual(msg.message_type, "event")
        self.assertAlmostEqual(msg.latitude, 24.7, places=6)

    def test_unknown_kind_is_stored_as_text(self):
        """A newer gateway may learn a kind before this module does; its body
        is still readable, so the message must not be lost over the label."""
        msg = self._receive("FUTURE", kind="hologram", body="something new")
        self.assertEqual(msg.message_type, "text")
        self.assertEqual(msg.body, "something new")

    def test_a_file_keeps_its_own_kind(self):
        """`kind` only speaks for messages with no file behind them."""
        msg = self._receive(
            "IMG1", kind="location", body="",
            media={"kind": "image", "mimetype": "image/jpeg", "filename": "a.jpg"})
        self.assertEqual(msg.message_type, "image")
        self.assertEqual(msg.media_state, "pending")

    def test_unreadable_coordinates_do_not_lose_the_message(self):
        msg = self._receive("LOCBAD", kind="location", body="📍 Somewhere",
                            location={"latitude": "north", "longitude": None})
        self.assertEqual(msg.message_type, "location")
        self.assertEqual(msg.latitude, 0)

    def test_real_location_copy_upgrades_a_placeholder(self):
        self._receive("LOCTWIN", body="[unsupported message type: text]",
                      placeholder=True)
        msg = self._receive(
            "LOCTWIN", kind="location", body="📍 Office",
            location={"latitude": 1.5, "longitude": 2.5})
        self.assertEqual(len(msg), 1)
        self.assertFalse(msg.is_placeholder)
        self.assertEqual(msg.message_type, "location")
        self.assertAlmostEqual(msg.longitude, 2.5, places=6)

    def test_filter_rule_can_match_a_rich_kind(self):
        self.env["whatsmeow.session.rule"].create({
            "session_id": self.session.id, "message_type": "poll",
            "action": "reject",
        })
        with mute_logger("odoo.addons.whatsmeow.controllers.webhook"):
            self.assertFalse(self._receive("POLL-NO", kind="poll", body="📊 Lunch?"))
        self.assertTrue(self._receive("TEXT-OK", body="hello"))

    def test_location_reaches_the_chatter_with_a_clickable_map_link(self):
        partner = self.env["res.partner"].create(
            {"name": "Map Sender", "phone": "+44 7700 900456"})
        self.ctrl._on_message(self.env, self.session, {
            "wa_message_id": "LOCCHAT", "sender_phone": "447700900456",
            "kind": "location",
            "body": "📍 Office\nhttps://maps.google.com/?q=1.500000,2.500000",
            "location": {"latitude": 1.5, "longitude": 2.5},
        })
        post = partner.message_ids.filtered(lambda m: m.message_type == "whatsmeow")
        self.assertEqual(len(post), 1)
        self.assertIn(
            '<a href="https://maps.google.com/?q=1.500000,2.500000"', post.body)

    def test_rich_kinds_cannot_be_sent(self):
        for kind in RICH_MESSAGE_TYPES:
            with self.assertRaises(ValidationError):
                self.env["whatsmeow.message"].create({
                    "session_id": self.session.id, "direction": "out",
                    "phone": "447700900123", "message_type": kind, "body": "x",
                })


@tagged("post_install", "-at_install")
class TestLinkify(WhatsmeowCommon):
    """Received text gets clickable links; the escaping guarantee must hold."""

    def _render(self, text):
        return str(render_markup(text, linkify=True))

    def test_off_by_default(self):
        """The composer preview's twin does not linkify, so neither does the
        default call — the two must keep agreeing."""
        self.assertEqual(str(render_markup("see https://a.example/x")),
                         "see https://a.example/x")

    def test_url_becomes_a_link(self):
        self.assertEqual(
            self._render("see https://a.example/x now"),
            'see <a href="https://a.example/x" target="_blank" '
            'rel="noreferrer noopener">https://a.example/x</a> now')

    def test_trailing_punctuation_stays_outside(self):
        html = self._render("Go to https://a.example/x.")
        self.assertIn('href="https://a.example/x"', html)
        self.assertTrue(html.endswith("</a>."))

    def test_underscores_in_a_url_are_not_italics(self):
        html = self._render("https://a.example/some_long_path and _this_")
        self.assertIn('href="https://a.example/some_long_path"', html)
        self.assertNotIn("some<em>", html)
        self.assertIn("<em>this</em>", html)

    def test_query_string_is_escaped_not_broken(self):
        html = self._render("https://a.example/?a=1&b=2")
        self.assertIn('href="https://a.example/?a=1&amp;b=2"', html)

    def test_a_url_cannot_break_out_of_its_attribute(self):
        html = self._render('https://a.example/"onmouseover="alert(1)')
        self.assertNotIn('"onmouseover', html)
        self.assertIn('href="https://a.example/"', html)
        html = self._render("https://a.example/<script>alert(1)</script>")
        self.assertNotIn("<script", html)

    def test_bare_scheme_is_left_alone(self):
        self.assertEqual(self._render("https:// is a scheme"), "https:// is a scheme")

    def test_links_inside_monospace_stay_literal(self):
        self.assertEqual(self._render("```https://a.example```"),
                         "<code>https://a.example</code>")


@tagged("post_install", "-at_install")
class TestGroups(WhatsmeowCommon):
    """A group's only address is its JID, which nobody knows by heart. The
    group directory lets a message be addressed by the group's name."""

    def setUp(self):
        super().setUp()
        self.ctrl = WhatsmeowWebhook()
        self.Group = self.env["whatsmeow.group"]
        self.group = self.Group.create({
            "session_id": self.session.id, "jid": GROUP_JID, "name": "Sales Team",
        })

    # -- sending ---------------------------------------------------------------
    def test_picking_a_group_addresses_the_message_to_it(self):
        msg = self.env["whatsmeow.message"].create({
            "session_id": self.session.id, "direction": "out",
            "group_id": self.group.id, "body": "Hello team",
        })
        self.assertEqual(msg.chat_jid, GROUP_JID)
        self.assertEqual(msg.chat_type, "group")
        self.assertEqual(msg.chat_name, "Sales Team")

        with patch.object(type(self.session), "_gw",
                          return_value={"wa_message_id": "G1"}) as gw:
            msg.action_send()
        path, payload = gw.call_args.args[1], gw.call_args.args[2]
        self.assertEqual(path, "/sessions/client_acme/send")
        self.assertEqual(payload["jid"], GROUP_JID)
        self.assertEqual(payload["phone"], "")
        self.assertEqual(payload["message"], "Hello team")
        self.assertEqual(msg.state, "sent")

    def test_a_group_jid_typed_by_hand_still_sends(self):
        """The directory is a convenience, not a gate: a group that has not
        been synchronised yet is still reachable by its JID."""
        msg = self.env["whatsmeow.message"].create({
            "session_id": self.session.id, "direction": "out",
            "chat_jid": "120363999999999999@g.us", "body": "Hi",
        })
        self.assertEqual(msg.chat_type, "group")
        self.assertFalse(msg.group_id)
        with patch.object(type(self.session), "_gw",
                          return_value={"wa_message_id": "G2"}) as gw:
            msg.action_send()
        self.assertEqual(gw.call_args.args[2]["jid"], "120363999999999999@g.us")

    def test_media_can_be_sent_to_a_group(self):
        msg = self.env["whatsmeow.message"].create({
            "session_id": self.session.id, "direction": "out",
            "group_id": self.group.id, "message_type": "image",
            "media_data": "aGVsbG8=", "media_filename": "a.png",
            "media_mimetype": "image/png",
        })
        with patch.object(type(self.session), "_gw",
                          return_value={"wa_message_id": "G3"}) as gw:
            msg.action_send()
        self.assertEqual(gw.call_args.args[1], "/sessions/client_acme/send-media")
        self.assertEqual(gw.call_args.args[2]["jid"], GROUP_JID)

    def test_setting_the_group_later_readdresses_the_message(self):
        msg = self.env["whatsmeow.message"].create({
            "session_id": self.session.id, "direction": "out",
            "phone": "447700900123", "body": "Hi",
        })
        msg.write({"group_id": self.group.id})
        self.assertEqual(msg.chat_jid, GROUP_JID)

    def test_form_fills_the_address_and_drops_the_private_recipient(self):
        partner = self.env["res.partner"].create({"name": "Someone"})
        form = Form(self.env["whatsmeow.message"])
        form.session_id = self.session
        form.partner_id = partner
        form.phone = "447700900123"
        form.body = "Hello team"
        form.group_id = self.group
        self.assertEqual(form.chat_jid, GROUP_JID)
        self.assertFalse(form.phone)
        self.assertFalse(form.partner_id)
        msg = form.save()
        self.assertEqual(msg.chat_type, "group")
        self.assertEqual(msg.group_id, self.group)

    def test_form_clearing_the_group_clears_its_address(self):
        form = Form(self.env["whatsmeow.message"])
        form.session_id = self.session
        form.group_id = self.group
        form.group_id = self.Group
        self.assertFalse(form.chat_jid)

    def test_group_of_another_session_is_refused(self):
        other = self.env["whatsmeow.session"].create({
            "name": "Other", "code": "other", "connection_id": self.connection.id,
        })
        with self.assertRaises(ValidationError):
            self.env["whatsmeow.message"].create({
                "session_id": other.id, "direction": "out",
                "group_id": self.group.id, "body": "Hi",
            })

    def test_group_and_a_different_jid_are_refused(self):
        with self.assertRaises(ValidationError):
            self.env["whatsmeow.message"].create({
                "session_id": self.session.id, "direction": "out",
                "group_id": self.group.id, "chat_jid": "120363111111111111@g.us",
                "body": "Hi",
            })

    def test_group_send_ignores_an_individual_opt_out(self):
        """One member cannot speak for a group — unchanged, but now reachable
        from a picker, so worth pinning."""
        self.env["res.partner"].create({
            "name": "Opted Out", "phone": "+44 7700 900123",
            "whatsmeow_optout": True,
        })
        msg = self.env["whatsmeow.message"].create({
            "session_id": self.session.id, "direction": "out",
            "group_id": self.group.id, "body": "Hello team",
        })
        with patch.object(type(self.session), "_gw",
                          return_value={"wa_message_id": "G4"}):
            msg.action_send()
        self.assertEqual(msg.state, "sent")

    def test_send_message_action_prefills_the_group(self):
        ctx = self.group.action_send_message()["context"]
        self.assertEqual(ctx["default_group_id"], self.group.id)
        self.assertEqual(ctx["default_chat_jid"], GROUP_JID)
        self.assertEqual(ctx["default_session_id"], self.session.id)

    def test_reply_to_a_group_message_keeps_the_group(self):
        self.ctrl._on_message(self.env, self.session, {
            "wa_message_id": "GIN1", "sender_phone": "447700900123",
            "chat_jid": GROUP_JID, "chat_name": "Sales Team", "is_group": True,
            "body": "anyone there?",
        })
        inbound = self.env["whatsmeow.message"].search([("wa_message_id", "=", "GIN1")])
        ctx = inbound.action_reply()["context"]
        self.assertEqual(ctx["default_group_id"], self.group.id)
        self.assertEqual(ctx["default_chat_jid"], GROUP_JID)

    # -- the directory ---------------------------------------------------------
    def test_a_group_is_unique_per_session(self):
        with self.assertRaises(IntegrityError), mute_logger("odoo.sql_db"):
            self.Group.create({
                "session_id": self.session.id, "jid": GROUP_JID, "name": "Dup",
            })

    def test_sync_creates_updates_and_archives(self):
        stale = self.Group.create({
            "session_id": self.session.id, "jid": "120363222222222222@g.us",
            "name": "Left Long Ago",
        })
        listing = {"groups": [
            {"jid": GROUP_JID, "name": "Sales Team EMEA", "participants": 12,
             "announce": True, "community": False},
            {"jid": "120363333333333333@g.us", "name": "", "participants": 3},
        ]}
        with patch.object(type(self.session), "_gw", return_value=listing) as gw:
            action = self.session.action_sync_groups()
        self.assertEqual(gw.call_args.args[:2], ("GET", "/sessions/client_acme/groups"))
        self.assertEqual(action["res_model"], "whatsmeow.group")

        self.assertEqual(self.group.name, "Sales Team EMEA")
        self.assertEqual(self.group.participant_count, 12)
        self.assertTrue(self.group.is_announce)
        self.assertTrue(self.group.last_sync)

        new = self.Group.search([("jid", "=", "120363333333333333@g.us")])
        self.assertEqual(new.session_id, self.session)
        self.assertEqual(new.name, new.jid, "an unnamed group is labelled by its JID")

        self.assertFalse(stale.active, "a group the listing omits has been left")
        self.assertEqual(self.session.group_count, 2)

    def test_sync_revives_a_group_the_number_rejoined(self):
        self.group.active = False
        with patch.object(type(self.session), "_gw", return_value={"groups": [
                {"jid": GROUP_JID, "name": "Sales Team"}]}):
            self.session.action_sync_groups()
        self.assertTrue(self.group.active)
        self.assertEqual(
            self.Group.with_context(active_test=False).search_count(
                [("jid", "=", GROUP_JID)]), 1)

    def test_sync_does_not_touch_another_sessions_groups(self):
        other = self.env["whatsmeow.session"].create({
            "name": "Other", "code": "other", "connection_id": self.connection.id,
        })
        theirs = self.Group.create({
            "session_id": other.id, "jid": GROUP_JID, "name": "Same group, their side",
        })
        with patch.object(type(self.session), "_gw", return_value={"groups": []}):
            self.session.action_sync_groups()
        self.assertFalse(self.group.active)
        self.assertTrue(theirs.active)

    def test_inbound_group_message_registers_the_group(self):
        jid = "120363444444444444@g.us"
        self.ctrl._on_message(self.env, self.session, {
            "wa_message_id": "GNEW1", "sender_phone": "447700900123",
            "chat_jid": jid, "chat_name": "Support", "is_group": True, "body": "hi",
        })
        group = self.Group.search([("jid", "=", jid)])
        self.assertEqual(group.name, "Support")
        self.assertEqual(group.session_id, self.session)
        msg = self.env["whatsmeow.message"].search([("wa_message_id", "=", "GNEW1")])
        self.assertEqual(msg.group_id, group)
        self.assertEqual(group.message_count, 1)

    def test_inbound_group_message_learns_a_rename_and_revives(self):
        self.group.active = False
        self.ctrl._on_message(self.env, self.session, {
            "wa_message_id": "GREN1", "sender_phone": "447700900123",
            "chat_jid": GROUP_JID, "chat_name": "Sales Team 2", "is_group": True,
            "body": "hi",
        })
        self.assertEqual(self.group.name, "Sales Team 2")
        self.assertTrue(self.group.active)

    def test_inbound_without_a_subject_keeps_the_known_name(self):
        """The gateway sends "" when WhatsApp would not tell it the subject."""
        self.ctrl._on_message(self.env, self.session, {
            "wa_message_id": "GNON1", "sender_phone": "447700900123",
            "chat_jid": GROUP_JID, "chat_name": "", "is_group": True, "body": "hi",
        })
        self.assertEqual(self.group.name, "Sales Team")

    def test_private_message_registers_no_group(self):
        before = self.Group.with_context(active_test=False).search_count([])
        self.ctrl._on_message(self.env, self.session, {
            "wa_message_id": "PRIV1", "sender_phone": "447700900123",
            "chat_jid": "447700900123@s.whatsapp.net", "body": "hi",
        })
        self.assertEqual(
            self.Group.with_context(active_test=False).search_count([]), before)

    def test_rejected_group_message_registers_no_group(self):
        """A filtered chat stores nothing — not a message, and not a directory
        entry inviting someone to write to the group that was filtered out."""
        jid = "120363555555555555@g.us"
        self.session.inbound_default = "reject"
        with mute_logger("odoo.addons.whatsmeow.controllers.webhook"):
            self.ctrl._on_message(self.env, self.session, {
                "wa_message_id": "GREJ1", "sender_phone": "447700900123",
                "chat_jid": jid, "chat_name": "Noise", "is_group": True, "body": "hi",
            })
        self.assertFalse(self.Group.search([("jid", "=", jid)]))
