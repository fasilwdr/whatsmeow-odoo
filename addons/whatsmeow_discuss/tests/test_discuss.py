import base64
from unittest.mock import patch

from odoo.exceptions import UserError
from odoo.tests import TransactionCase, tagged
from odoo.tools import mute_logger

from odoo.addons.whatsmeow.controllers.webhook import WhatsmeowWebhook


class DiscussCommon(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.connection = cls.env["whatsmeow.connection"].create({
            "name": "GW", "base_url": "http://127.0.0.1:8080",
            "api_key": "k", "webhook_secret": "s",
        })
        cls.session = cls.env["whatsmeow.session"].create({
            "name": "ACME", "code": "acme", "connection_id": cls.connection.id,
            "route_to_discuss": True,
        })
        cls.frontdesk = cls._user("frontdesk")
        cls.manager = cls._user("manager")
        cls.ctrl = WhatsmeowWebhook()

    @classmethod
    def _user(cls, login):
        return cls.env["res.users"].create({
            "name": login.title(), "login": login,
            "groups_id": [(6, 0, [cls.env.ref("base.group_user").id])],
        })

    def _inbound(self, **data):
        """Drive one inbound message through the webhook, as the gateway would."""
        payload = {
            "wa_message_id": data.pop("wa_message_id", "WA1"),
            "sender_phone": "447700900001",
            "sender_jid": "447700900001@s.whatsapp.net",
            "chat_jid": "447700900001@s.whatsapp.net",
            "body": "hello",
        }
        payload.update(data)
        with mute_logger("odoo.addons.whatsmeow.controllers.webhook"):
            self.ctrl._on_message(self.env, self.session, payload)

    def _channels(self):
        return self.env["mail.channel"].search([
            ("channel_type", "=", "whatsmeow"),
            ("whatsmeow_session_id", "=", self.session.id),
        ])


@tagged("post_install", "-at_install")
class TestRoutingEngine(DiscussCommon):

    def _facts(self, **over):
        facts = {
            "chat_type": "private", "message_type": "text",
            "partner_id": False, "sender_state": "new", "chat_jid": "j",
            "phone_tail": "", "sender_lid": "", "body": "", "is_placeholder": False,
        }
        facts.update(over)
        return facts

    def test_first_match_wins(self):
        self.env["whatsmeow.route"].create({
            "session_id": self.session.id, "sequence": 10,
            "keyword": "invoice", "user_ids": [(6, 0, self.manager.ids)],
        })
        self.env["whatsmeow.route"].create({
            "session_id": self.session.id, "sequence": 20,
            "user_ids": [(6, 0, self.frontdesk.ids)],
        })
        # the keyword route wins for an invoice question...
        self.assertEqual(
            self.session._route_users(self._facts(body="my invoice")),
            self.manager)
        # ...but anything else falls to the catch-all
        self.assertEqual(
            self.session._route_users(self._facts(body="hi")),
            self.frontdesk)

    def test_no_match_uses_fallback(self):
        self.session.route_fallback_user_ids = [(6, 0, self.frontdesk.ids)]
        self.env["whatsmeow.route"].create({
            "session_id": self.session.id, "keyword": "nope",
            "user_ids": [(6, 0, self.manager.ids)],
        })
        self.assertEqual(
            self.session._route_users(self._facts(body="hi")),
            self.frontdesk)

    def test_inactive_route_skipped(self):
        self.env["whatsmeow.route"].create({
            "session_id": self.session.id, "active": False,
            "user_ids": [(6, 0, self.manager.ids)],
        })
        self.session.route_fallback_user_ids = [(6, 0, self.frontdesk.ids)]
        self.assertEqual(
            self.session._route_users(self._facts()), self.frontdesk)

    def test_sender_state_new_existing_blank(self):
        route = self.env["whatsmeow.route"].create({"session_id": self.session.id})
        route.sender_state = "new"
        self.assertTrue(route._matches(self._facts(sender_state="new")))
        self.assertFalse(route._matches(self._facts(sender_state="existing")))
        route.sender_state = "existing"
        self.assertTrue(route._matches(self._facts(sender_state="existing")))
        route.sender_state = False  # blank matches any
        self.assertTrue(route._matches(self._facts(sender_state="new")))
        self.assertTrue(route._matches(self._facts(sender_state="existing")))


@tagged("post_install", "-at_install")
class TestInboundToChannel(DiscussCommon):

    def test_creates_channel_routes_and_posts(self):
        self.session.route_fallback_user_ids = [(6, 0, self.frontdesk.ids)]
        self._inbound(push_name="Alice", body="hi there")

        channel = self._channels()
        self.assertEqual(len(channel), 1)
        # operators are members; the correspondent is the author, not a member
        self.assertEqual(channel.channel_member_ids.partner_id,
                         self.frontdesk.partner_id)
        self.assertTrue(channel.whatsmeow_partner_id)
        self.assertNotIn(channel.whatsmeow_partner_id,
                         channel.channel_member_ids.partner_id)
        # the inbound landed as a bubble authored by the correspondent
        posted = channel.message_ids.filtered(lambda m: m.message_type == "comment")
        self.assertTrue(posted)
        self.assertEqual(posted.author_id, channel.whatsmeow_partner_id)
        self.assertIn("hi there", posted.body)

    def test_unknown_sender_auto_creates_partner(self):
        self._inbound(sender_phone="447700900777", push_name="Bob")
        channel = self._channels()
        self.assertEqual(channel.whatsmeow_partner_id.name, "Bob")
        self.assertEqual(channel.whatsmeow_partner_id.phone, "447700900777")
        # the very first message must carry the auto-created partner too, not
        # only the messages that follow (which resolve by phone)
        first = self.env["whatsmeow.message"].search([
            ("session_id", "=", self.session.id), ("direction", "=", "in")])
        self.assertEqual(first.partner_id, channel.whatsmeow_partner_id)

    def test_auto_created_sender_still_routes_as_new(self):
        # auto-creating a partner must not reclassify a first-time sender as
        # "existing" and misroute them
        self.env["whatsmeow.route"].create({
            "session_id": self.session.id, "sequence": 10,
            "sender_state": "new", "user_ids": [(6, 0, self.frontdesk.ids)],
        })
        self.env["whatsmeow.route"].create({
            "session_id": self.session.id, "sequence": 20,
            "sender_state": "existing", "user_ids": [(6, 0, self.manager.ids)],
        })
        self._inbound(sender_phone="447700900888", push_name="New Guy")
        channel = self._channels()
        self.assertEqual(channel.channel_member_ids.partner_id,
                         self.frontdesk.partner_id)

    def test_lid_only_sender_falls_back_to_generic(self):
        # no phone, auto-create cannot make a meaningful partner
        self._inbound(sender_lid="12345", sender_phone="")
        channel = self._channels()
        self.assertEqual(len(channel), 1)
        self.assertFalse(channel.whatsmeow_partner_id)

    def test_second_message_reuses_channel_and_keeps_members(self):
        self.session.route_fallback_user_ids = [(6, 0, self.frontdesk.ids)]
        self._inbound(wa_message_id="A")
        channel = self._channels()
        # a manual reassignment the second message must not clobber
        channel.add_members(partner_ids=self.manager.partner_id.ids)
        self._inbound(wa_message_id="B")

        self.assertEqual(self._channels(), channel)          # still one channel
        self.assertIn(self.manager.partner_id,
                      channel.channel_member_ids.partner_id)  # not re-routed away
        self.assertEqual(len(channel.message_ids.filtered(
            lambda m: m.message_type == "comment")), 2)

    def test_voice_note_posts_its_audio_attachment(self):
        # Odoo 16 has no inline voice player (`discuss.voice.metadata` arrived
        # in 17), so a voice note posts as an ordinary audio attachment. What
        # must hold here is that the audio reaches the conversation at all, and
        # that the message still records it was a voice note.
        msg = self.env["whatsmeow.message"].create({
            "session_id": self.session.id, "direction": "in", "state": "received",
            "chat_jid": "447700900001@s.whatsapp.net", "phone": "447700900001",
            "message_type": "audio", "is_voice_note": True,
            "media_data": base64.b64encode(b"OggS fake voice"),
            "media_filename": "voice.ogg", "media_mimetype": "audio/ogg",
            "media_state": "fetched", "wa_message_id": "VN1",
        })
        msg._deliver_inbound()
        posted = self._channels().message_ids.filtered(lambda m: m.attachment_ids)
        self.assertEqual(posted.attachment_ids.mimetype, "audio/ogg")
        self.assertEqual(posted.attachment_ids.name, "voice.ogg")
        self.assertTrue(msg.is_voice_note)

    def test_routing_off_posts_to_chatter_and_makes_no_channel(self):
        self.session.route_to_discuss = False
        partner = self.env["res.partner"].create({
            "name": "Carol", "phone": "447700900001"})
        before = len(partner.message_ids)
        self._inbound(sender_phone="447700900001")
        self.assertFalse(self._channels())
        self.assertEqual(len(partner.message_ids), before + 1)


@tagged("post_install", "-at_install")
class TestReactions(DiscussCommon):

    def setUp(self):
        super().setUp()
        self.session.route_fallback_user_ids = [(6, 0, self.frontdesk.ids)]
        self._inbound(sender_phone="447700900001")
        self.channel = self._channels()
        self.inbound = self.env["whatsmeow.message"].search([
            ("session_id", "=", self.session.id), ("direction", "=", "in")], limit=1)
        self.bubble = self.inbound.mail_message_id

    def _react_inbound(self, emoji, **over):
        data = {
            "target_id": self.inbound.wa_message_id,
            "emoji": emoji,
            "sender_phone": "447700900001",
        }
        data.update(over)
        with mute_logger("odoo.addons.whatsmeow.controllers.webhook"):
            self.ctrl._on_reaction(self.env, self.session, data)

    def _reactions(self):
        return self.env["mail.message.reaction"].search(
            [("message_id", "=", self.bubble.id)])

    def test_inbound_reaction_lands_on_the_bubble(self):
        self._react_inbound("👍")
        reactions = self._reactions()
        self.assertEqual(reactions.content, "👍")
        self.assertEqual(reactions.partner_id, self.channel.whatsmeow_partner_id)

    def test_inbound_reaction_change_replaces_previous(self):
        self._react_inbound("👍")
        self._react_inbound("❤️")
        # WhatsApp keeps one reaction per sender — the heart replaced the thumb
        self.assertEqual(self._reactions().content, "❤️")

    def test_inbound_reaction_removal_clears_it(self):
        self._react_inbound("👍")
        self._react_inbound("")  # empty emoji = removed
        self.assertFalse(self._reactions())

    def test_applying_an_inbound_reaction_does_not_relay_out(self):
        with patch.object(type(self.session), "_gw") as gw:
            self._react_inbound("👍")
        self.assertFalse(gw.called)

    def test_operator_reaction_is_sent_over_whatsapp(self):
        with patch.object(type(self.session), "_gw",
                          return_value={"wa_message_id": "R1"}) as gw:
            self.bubble.with_user(self.frontdesk)._message_add_reaction("🎉")
        self.assertTrue(gw.called)
        path, payload = gw.call_args.args[1], gw.call_args.args[2]
        self.assertIn("/react", path)
        self.assertEqual(payload["target_id"], self.inbound.wa_message_id)
        self.assertEqual(payload["emoji"], "🎉")
        self.assertFalse(payload["from_me"])  # reacting to the contact's message

    def test_operator_removing_a_reaction_clears_it_over_whatsapp(self):
        with patch.object(type(self.session), "_gw",
                          return_value={"wa_message_id": "R1"}):
            self.bubble.with_user(self.frontdesk)._message_add_reaction("🎉")
        with patch.object(type(self.session), "_gw",
                          return_value={"wa_message_id": "R2"}) as gw:
            self.bubble.with_user(self.frontdesk)._message_remove_reaction("🎉")
        self.assertEqual(gw.call_args.args[2]["emoji"], "")

    def test_reaction_to_own_reply_is_flagged_from_me(self):
        # an operator reply, then a reaction on that outgoing bubble
        with patch.object(type(self.session), "_gw",
                          return_value={"wa_message_id": "OUT1"}):
            self.channel.with_user(self.frontdesk).message_post(
                body="on it", message_type="comment", subtype_xmlid="mail.mt_comment")
        reply = self.env["whatsmeow.message"].search([
            ("session_id", "=", self.session.id), ("direction", "=", "out")], limit=1)
        with patch.object(type(self.session), "_gw",
                          return_value={"wa_message_id": "R1"}) as gw:
            reply.mail_message_id.with_user(self.frontdesk)._message_add_reaction("👍")
        self.assertTrue(gw.call_args.args[2]["from_me"])


@tagged("post_install", "-at_install")
class TestOutboundRelay(DiscussCommon):

    def _open_channel(self):
        self.session.route_fallback_user_ids = [(6, 0, self.frontdesk.ids)]
        self._inbound()
        return self._channels()

    def test_operator_reply_sends_over_whatsapp(self):
        channel = self._open_channel()
        with patch.object(type(self.session), "_gw",
                          return_value={"wa_message_id": "OUT1"}) as gw:
            channel.with_user(self.frontdesk).message_post(
                body="on it", message_type="comment",
                subtype_xmlid="mail.mt_comment")
        out = self.env["whatsmeow.message"].search([
            ("session_id", "=", self.session.id), ("direction", "=", "out")])
        self.assertEqual(len(out), 1)
        self.assertEqual(out.chat_jid, channel.whatsmeow_chat_jid)
        self.assertEqual(out.state, "sent")
        self.assertEqual(out.mail_message_id.body and "on it" in out.mail_message_id.body, True)
        self.assertTrue(gw.called)

    def test_reply_to_a_bubble_quotes_it_on_whatsapp(self):
        channel = self._open_channel()
        inbound = self.env["whatsmeow.message"].search([
            ("session_id", "=", self.session.id), ("direction", "=", "in")], limit=1)
        self.assertTrue(inbound.mail_message_id)  # the bubble to reply to
        with patch.object(type(self.session), "_gw",
                          return_value={"wa_message_id": "OUT1"}):
            channel.with_user(self.frontdesk).message_post(
                body="answering that", message_type="comment",
                subtype_xmlid="mail.mt_comment",
                parent_id=inbound.mail_message_id.id)
        out = self.env["whatsmeow.message"].search([
            ("session_id", "=", self.session.id), ("direction", "=", "out")])
        # the outgoing carries the quote, so the gateway sends a real WA reply
        self.assertEqual(out.reply_to_id, inbound)
        self.assertEqual(out._quote_payload().get("quoted_id"),
                         inbound.wa_message_id)

    def test_inbound_reply_threads_under_the_bubble_it_quotes(self):
        """A contact quoting a message must render in Discuss's native reply
        style, i.e. as a child of the bubble it answers."""
        channel = self._open_channel()
        first = self.env["whatsmeow.message"].search([
            ("session_id", "=", self.session.id), ("direction", "=", "in")], limit=1)
        self._inbound(wa_message_id="WA2", body="about that",
                      quoted_id=first.wa_message_id)
        reply = self.env["whatsmeow.message"].search(
            [("wa_message_id", "=", "WA2")])
        self.assertEqual(reply.reply_to_id, first)
        self.assertEqual(reply.mail_message_id.parent_id, first.mail_message_id)
        self.assertEqual(reply.mail_message_id.res_id, channel.id)

    def test_inbound_reply_to_an_unknown_message_still_posts(self):
        """The quoted message may predate the install or have been filtered
        out; the reply must land as a plain bubble, not vanish."""
        self._open_channel()
        self._inbound(wa_message_id="WA2", body="about that",
                      quoted_id="NEVER-SEEN")
        reply = self.env["whatsmeow.message"].search(
            [("wa_message_id", "=", "WA2")])
        self.assertFalse(reply.reply_to_id)
        self.assertTrue(reply.mail_message_id)
        self.assertFalse(reply.mail_message_id.parent_id)

    def test_plain_reply_carries_no_quote(self):
        channel = self._open_channel()
        with patch.object(type(self.session), "_gw",
                          return_value={"wa_message_id": "OUT1"}):
            channel.with_user(self.frontdesk).message_post(
                body="just a message", message_type="comment",
                subtype_xmlid="mail.mt_comment")
        out = self.env["whatsmeow.message"].search([
            ("session_id", "=", self.session.id), ("direction", "=", "out")])
        self.assertFalse(out.reply_to_id)

    def test_own_inbound_post_is_not_relayed(self):
        # opening the channel already posted the inbound bubble; it must not
        # have produced an outgoing message
        self._open_channel()
        out = self.env["whatsmeow.message"].search([
            ("session_id", "=", self.session.id), ("direction", "=", "out")])
        self.assertFalse(out)

    def test_non_comment_is_not_relayed(self):
        channel = self._open_channel()
        with patch.object(type(self.session), "_gw") as gw:
            channel.with_user(self.frontdesk).message_post(
                body="note", message_type="notification",
                subtype_xmlid="mail.mt_note")
        self.assertFalse(gw.called)
        self.assertFalse(self.env["whatsmeow.message"].search([
            ("session_id", "=", self.session.id), ("direction", "=", "out")]))

    def test_gateway_failure_notes_channel_without_losing_reply(self):
        channel = self._open_channel()
        with patch.object(type(self.session), "_gw",
                          side_effect=UserError("gateway down")), \
                mute_logger("odoo.addons.whatsmeow.models.whatsmeow_message",
                            "odoo.addons.whatsmeow_discuss.models.mail_channel"):
            channel.with_user(self.frontdesk).message_post(
                body="please reply", message_type="comment",
                subtype_xmlid="mail.mt_comment")
        # the operator's own message survived
        self.assertTrue(channel.message_ids.filtered(
            lambda m: m.body and "please reply" in m.body))
        # a failure note was posted back into the channel
        self.assertTrue(channel.message_ids.filtered(
            lambda m: m.body and "could not send" in m.body))
        # the outgoing record is marked errored, not sent
        out = self.env["whatsmeow.message"].search([
            ("session_id", "=", self.session.id), ("direction", "=", "out")])
        self.assertEqual(out.state, "error")


@tagged("post_install", "-at_install")
class TestRelayOptOut(DiscussCommon):
    """An operator typing in the thread is a send like any other: the opt-out
    gate (core, PLAN.md §12.3) applies there too, and a blocked reply says so
    in the channel instead of vanishing."""

    def test_a_reply_to_an_opted_out_contact_does_not_go_out(self):
        contact = self.env["res.partner"].create({
            "name": "Alice", "phone": "+44 7700 900001", "whatsmeow_optout": True,
        })
        self.session.route_fallback_user_ids = [(6, 0, self.frontdesk.ids)]
        self._inbound()
        channel = self._channels()
        self.assertEqual(channel.whatsmeow_partner_id, contact)

        with patch.object(type(self.session), "_gw") as gw, \
                mute_logger("odoo.addons.whatsmeow_discuss.models.mail_channel"):
            channel.with_user(self.frontdesk).message_post(
                body="are you there?", message_type="comment",
                subtype_xmlid="mail.mt_comment",
            )
        self.assertEqual(gw.call_count, 0, "nothing may reach WhatsApp")
        note = self.env["mail.message"].search(
            [("model", "=", "mail.channel"), ("res_id", "=", channel.id)],
            order="id desc", limit=1,
        )
        self.assertIn("opted out", note.body)
        self.assertIn("could not send", note.body)
