import base64
from datetime import timedelta
from io import BytesIO
from unittest.mock import patch

from openpyxl import Workbook

from odoo import fields
from odoo.exceptions import AccessError, UserError
from odoo.tests import TransactionCase, tagged
from odoo.tools import mute_logger
from odoo.tools.misc import file_path

from odoo.addons.whatsmeow.controllers.webhook import WhatsmeowWebhook
from odoo.addons.whatsmeow_marketing.models.whatsmeow_marketing_campaign import (
    MARKETING_BATCH,
)


class MarketingCommon(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.connection = cls.env["whatsmeow.connection"].create({
            "name": "GW", "base_url": "http://127.0.0.1:8080",
            "api_key": "k", "webhook_secret": "s-marketing",
        })
        cls.session = cls.env["whatsmeow.session"].create({
            "name": "ACME", "code": "acme_mkt", "connection_id": cls.connection.id,
            # The caps have their own tests; most tests here are about the
            # ledger, and a day-1 allowance of 20 would silently truncate them.
            "warmup_enabled": False,
        })
        cls.ctrl = WhatsmeowWebhook()
        cls.marketer = cls._user("marketer")
        cls.other = cls._user("other_marketer")

    @classmethod
    def _user(cls, login):
        return cls.env["res.users"].create({
            "name": login.title(), "login": login,
            "group_ids": [(6, 0, [
                cls.env.ref("base.group_user").id,
                cls.env.ref("whatsmeow_marketing.group_whatsmeow_marketing_user").id,
            ])],
        })

    @classmethod
    def _contact(cls, name, phone, **vals):
        return cls.env["whatsmeow.broadcast.contact"].create({
            "name": name, "phone": phone, **vals,
        })

    @classmethod
    def _partner(cls, name, phone, **vals):
        return cls.env["res.partner"].create({"name": name, "phone": phone, **vals})

    def _campaign(self, **vals):
        base = {
            "name": "Blast",
            "session_id": self.session.id,
            "body": "Hello {{ object.name }}",
            "recipient_source": "list",
        }
        base.update(vals)
        return self.env["whatsmeow.marketing.campaign"].create(base)

    def _list(self, name, contacts):
        return self.env["whatsmeow.broadcast.list"].create({
            "name": name, "contact_ids": [(6, 0, contacts.ids)],
        })

    def _gateway(self):
        """Patch the gateway so a send succeeds with a fresh WhatsApp id."""
        counter = {"n": 0}

        def _request(self_conn, method, path, payload=None, **kwargs):
            counter["n"] += 1
            return {"wa_message_id": f"WA{counter['n']}"}

        return patch.object(type(self.connection), "_request", _request)

    def _send_queued(self, campaign):
        """Hand everything this campaign has queued to the (patched) gateway."""
        messages = campaign.trace_ids.message_id.filtered(
            lambda m: m.state == "outgoing")
        with self._gateway():
            messages.action_send()
        return messages

    def _inbound(self, **data):
        payload = {
            "wa_message_id": "WAIN1",
            "sender_phone": "447700900001",
            "sender_jid": "447700900001@s.whatsapp.net",
            "chat_jid": "447700900001@s.whatsapp.net",
            "body": "hello",
        }
        payload.update(data)
        with mute_logger("odoo.addons.whatsmeow.controllers.webhook",
                         "odoo.addons.whatsmeow_marketing.models.whatsmeow_session",
                         "odoo.addons.whatsmeow_marketing.models.whatsmeow_message"):
            self.ctrl._on_message(self.env, self.session, payload)


@tagged("post_install", "-at_install")
class TestRecipients(MarketingCommon):

    def test_broadcast_list_source(self):
        contacts = self._contact("Sara", "+44 7700 900101") \
            | self._contact("Omar", "447700900102")
        campaign = self._campaign(broadcast_list_ids=[(6, 0, self._list("L", contacts).ids)])
        self.assertEqual(campaign._recipient_records(), contacts)
        self.assertEqual(campaign.recipient_preview_count, 2)

    def test_partner_domain_source(self):
        wanted = self._partner("Wanted", "447700900201", ref="wanted")
        self._partner("Unwanted", "447700900202")
        campaign = self._campaign(
            recipient_source="domain",
            mailing_domain="[('ref', '=', 'wanted')]",
        )
        self.assertEqual(campaign._recipient_records(), wanted)

    def test_dynamic_list_copies_model_and_domain(self):
        """A dynamic list is a starting point, not a live link: what was sent
        must stay legible even if someone edits the filter afterwards."""
        wanted = self._partner("Filtered", "447700900301", ref="dyn")
        filt = self.env["whatsmeow.marketing.filter"].create({
            "name": "Dyn",
            "model_id": self.env["ir.model"]._get("res.partner").id,
            "mailing_domain": "[('ref', '=', 'dyn')]",
        })
        campaign = self._campaign(recipient_source="filter")
        campaign.filter_id = filt
        campaign._onchange_filter_id()
        self.assertEqual(campaign.mailing_domain, "[('ref', '=', 'dyn')]")
        self.assertEqual(campaign._recipient_records(), wanted)
        filt.mailing_domain = "[]"
        self.assertEqual(campaign._recipient_records(), wanted,
                         "the campaign kept its own copy of the audience")

    def test_same_number_twice_is_one_send(self):
        """The same person in the audience twice must get one message: two
        copies of a blast is exactly what gets a number reported."""
        self._partner("Dup A", "+44 7700 900401", ref="dup")
        self._partner("Dup B", "447700900401", ref="dup")
        campaign = self._campaign(
            recipient_source="domain", mailing_domain="[('ref', '=', 'dup')]")
        traces = campaign._resolve_recipients()
        self.assertEqual(len(traces), 2)
        self.assertEqual(len(traces.filtered(lambda t: not t.skip_reason)), 1)
        self.assertEqual(traces.filtered("skip_reason").skip_reason, "duplicate")

    def test_skip_reasons(self):
        stopped = self._partner("Stopped", "447700900501", ref="skip")
        stopped.whatsmeow_marketing_optout = True
        blocked = self._partner("Blocked", "447700900502", ref="skip")
        blocked.whatsmeow_optout = True
        dead = self._partner("Dead", "447700900503", ref="skip")
        dead.write({
            "whatsmeow_registered": "no",
            "whatsmeow_registered_date": fields.Datetime.now(),
        })
        nophone = self._partner("No Phone", False, ref="skip")
        good = self._partner("Good", "447700900504", ref="skip")
        campaign = self._campaign(
            recipient_source="domain", mailing_domain="[('ref', '=', 'skip')]")
        campaign._resolve_recipients()
        reasons = {t.partner_id: t.skip_reason for t in campaign.trace_ids}
        self.assertEqual(reasons[stopped], "optout")
        self.assertEqual(reasons[blocked], "optout")
        self.assertEqual(reasons[dead], "not_registered")
        self.assertEqual(reasons[nophone], "no_phone")
        self.assertFalse(reasons[good])

    def test_stale_registration_answer_no_longer_blocks(self):
        """Past the TTL the gateway asks WhatsApp again, so a stale 'no' must
        stop blocking here too — core's rule, kept in step."""
        dead = self._partner("Old No", "447700900601", ref="stale")
        dead.write({
            "whatsmeow_registered": "no",
            "whatsmeow_registered_date": fields.Datetime.now() - timedelta(days=40),
        })
        campaign = self._campaign(
            recipient_source="domain", mailing_domain="[('ref', '=', 'stale')]")
        campaign._resolve_recipients()
        self.assertFalse(campaign.trace_ids.skip_reason)

    def test_optout_contact_never_resolves(self):
        contacts = self._contact("Ok", "447700900701") \
            | self._contact("Gone", "447700900702", optout=True)
        campaign = self._campaign(
            broadcast_list_ids=[(6, 0, self._list("L2", contacts).ids)])
        self.assertEqual(len(campaign._recipient_records()), 1)


@tagged("post_install", "-at_install")
class TestSending(MarketingCommon):

    def _campaign_of(self, count, **vals):
        """A campaign of `count` recipients, on a list of its own.

        The tag keeps names and numbers apart when one test builds two
        campaigns: broadcast contacts are unique per number per owner, and
        lists per name per owner.
        """
        tag = vals.get("name", "L")
        contacts = self.env["whatsmeow.broadcast.contact"]
        for index in range(count):
            contacts |= self._contact(
                f"{tag}{index}", f"4477{len(tag):02d}9{index:05d}")
        return self._campaign(
            broadcast_list_ids=[(6, 0, self._list(f"{tag}-{count}", contacts).ids)],
            **vals)

    def test_send_creates_traces_and_first_batch(self):
        campaign = self._campaign_of(3)
        campaign.action_send()
        self.assertEqual(campaign.state, "sending")
        self.assertEqual(campaign.total_count, 3)
        self.assertEqual(len(campaign.trace_ids.message_id), 3)
        self.assertEqual(set(campaign.trace_ids.mapped("state")), {"queued"})
        message = campaign.trace_ids[0].message_id
        self.assertEqual(message.direction, "out")
        self.assertEqual(message.state, "outgoing")
        self.assertEqual(message.marketing_campaign_id, campaign)

    def test_body_is_rendered_per_recipient(self):
        campaign = self._campaign_of(2)
        campaign.action_send()
        bodies = set(campaign.trace_ids.message_id.mapped("body"))
        self.assertEqual(bodies, {"Hello L0", "Hello L1"})

    def test_drip_never_exceeds_the_daily_share(self):
        """A blast must not eat the number's whole day: the cap bounds it, and
        the share leaves room for transactional traffic."""
        self.session.write({
            "warmup_enabled": True, "daily_cap_base": 20, "daily_cap_growth": 1.0,
            "daily_cap_max": 20, "marketing_daily_share": 80,
        })
        campaign = self._campaign_of(30)
        campaign.action_send()
        # 80% of a 20/day allowance
        self.assertEqual(len(campaign.trace_ids.message_id), 16)
        self.assertEqual(campaign.pending_count, 30)
        self.assertEqual(campaign.state, "sending")

    def test_queue_backlog_bounds_the_batch(self):
        """Whatever is still waiting counts against the next batch, so a
        stalled gateway cannot make the shared queue grow without limit."""
        campaign = self._campaign_of(MARKETING_BATCH + 10)
        campaign.action_send()
        self.assertEqual(len(campaign.trace_ids.message_id), MARKETING_BATCH)
        # nothing has been sent, so a second pass adds nothing
        self.env["whatsmeow.marketing.campaign"].cron_process_marketing()
        self.assertEqual(len(campaign.trace_ids.message_id), MARKETING_BATCH)

    def test_cron_drains_the_rest_and_finishes(self):
        campaign = self._campaign_of(3)
        campaign.action_send()
        self._send_queued(campaign)
        self.env["whatsmeow.marketing.campaign"].cron_process_marketing()
        self.assertEqual(campaign.state, "sent")
        self.assertTrue(campaign.sent_date)
        self.assertEqual(campaign.sent_count, 3)

    def test_campaigns_on_one_number_share_the_headroom(self):
        """A 5,000-recipient blast must not block a small campaign behind it."""
        self.session.write({
            "warmup_enabled": True, "daily_cap_base": 10, "daily_cap_growth": 1.0,
            "daily_cap_max": 10, "marketing_daily_share": 100,
        })
        big = self._campaign_of(20, name="Big")
        small = self._campaign_of(20, name="Small")
        big.write({"state": "sending"})
        small.write({"state": "sending"})
        big._resolve_recipients()
        small._resolve_recipients()
        self.env["whatsmeow.marketing.campaign"].cron_process_marketing()
        self.assertEqual(len(big.trace_ids.message_id), 5)
        self.assertEqual(len(small.trace_ids.message_id), 5)

    def test_scheduled_campaign_starts_itself(self):
        campaign = self._campaign_of(2)
        campaign.scheduled_date = fields.Datetime.now() - timedelta(minutes=1)
        self.env["whatsmeow.marketing.campaign"].cron_process_marketing()
        self.assertEqual(campaign.state, "sending")
        self.assertEqual(len(campaign.trace_ids.message_id), 2)

    def test_cancel_keeps_what_was_sent_and_drops_the_rest(self):
        campaign = self._campaign_of(4)
        campaign.action_send()
        sent = campaign.trace_ids[0]
        with self._gateway():
            sent.message_id.action_send()
        campaign.action_cancel()
        self.assertEqual(campaign.state, "cancelled")
        self.assertEqual(sent.state, "sent")
        others = campaign.trace_ids - sent
        self.assertEqual(set(others.mapped("state")), {"cancelled"})
        self.assertFalse(others.mapped("message_id"),
                         "a queued message WhatsApp never saw is deleted")

    def test_reset_to_draft_does_not_message_anyone_twice(self):
        campaign = self._campaign_of(3)
        campaign.action_send()
        sent = campaign.trace_ids[0]
        with self._gateway():
            sent.message_id.action_send()
        campaign.action_cancel()
        campaign.action_reset_draft()
        self.assertEqual(campaign.state, "draft")
        self.assertEqual(len(campaign.trace_ids), 1, "only the sent trace survives")
        campaign.action_send()
        recipients = campaign.trace_ids.filtered(lambda t: not t.skip_reason)
        self.assertEqual(len(recipients), 3)
        self.assertEqual(len(recipients.filtered(
            lambda t: t.phone_tail == sent.phone_tail)), 1)

    def test_send_refuses_when_nobody_can_be_messaged(self):
        contact = self._contact("Gone", "447700901999", optout=True)
        campaign = self._campaign(
            broadcast_list_ids=[(6, 0, self._list("L0", contact).ids)])
        with self.assertRaises(UserError):
            campaign.action_send()

    def test_only_a_draft_can_be_sent(self):
        campaign = self._campaign_of(1)
        campaign.action_send()
        with self.assertRaises(UserError):
            campaign.action_send()

    def test_media_campaign_sends_the_file(self):
        attachment = self.env["ir.attachment"].create({
            "name": "flyer.png", "datas": b"aGVsbG8=", "mimetype": "image/png",
        })
        campaign = self._campaign_of(1, attachment_ids=[(6, 0, attachment.ids)])
        campaign.action_send()
        message = campaign.trace_ids.message_id
        self.assertEqual(message.message_type, "image")
        self.assertEqual(message.media_filename, "flyer.png")


@tagged("post_install", "-at_install")
class TestTraceState(MarketingCommon):

    def setUp(self):
        super().setUp()
        self.contact = self._contact("Sara", "447700900001")
        self.campaign = self._campaign(
            broadcast_list_ids=[(6, 0, self._list("L", self.contact).ids)])

    def test_state_follows_the_message(self):
        self.campaign.action_send()
        trace = self.campaign.trace_ids
        self.assertEqual(trace.state, "queued")
        self._send_queued(self.campaign)
        self.assertEqual(trace.state, "sent")
        wa_id = trace.message_id.wa_message_id
        self.ctrl._on_receipt(self.env, {
            "receipt_type": "delivered", "wa_message_ids": [wa_id]})
        self.assertEqual(trace.state, "delivered")
        self.ctrl._on_receipt(self.env, {
            "receipt_type": "read", "wa_message_ids": [wa_id]})
        self.assertEqual(trace.state, "read")
        # Read implies delivered implies sent: the ladder counts upwards, so
        # "Sent" cannot fall as receipts arrive.
        self.assertEqual(
            (self.campaign.sent_count, self.campaign.delivered_count,
             self.campaign.read_count), (1, 1, 1))

    def test_failed_send_shows_the_reason(self):
        self.campaign.action_send()
        trace = self.campaign.trace_ids
        with patch.object(type(self.connection), "_request",
                          side_effect=Exception("gateway down")), \
                mute_logger("odoo.addons.whatsmeow.models.whatsmeow_message"):
            trace.message_id.action_send()
        self.assertEqual(trace.state, "failed")
        self.assertIn("gateway down", trace.failure_reason)
        self.assertEqual(self.campaign.failed_count, 1)


@tagged("post_install", "-at_install")
class TestStopStart(MarketingCommon):

    def setUp(self):
        super().setUp()
        self.partner = self._partner("Sara", "+44 7700 900001")
        self.contact = self._contact("Sara", "447700900001")

    def test_stop_flags_both_identities(self):
        """A person is routinely a partner *and* an imported broadcast contact;
        stopping only one of them means the next campaign reaches them anyway."""
        self._inbound(body="/stop")
        self.assertTrue(self.partner.whatsmeow_marketing_optout)
        self.assertTrue(self.contact.optout)
        self.assertIn("/stop", self.contact.optout_reason)

    def test_start_restores(self):
        self._inbound(body="/stop")
        self._inbound(wa_message_id="WAIN2", body="/start")
        self.assertFalse(self.partner.whatsmeow_marketing_optout)
        self.assertFalse(self.contact.optout)

    def test_keyword_must_be_the_whole_message(self):
        """A substring match would unsubscribe an enthusiastic customer."""
        self._inbound(body="please don't stop sending these, they're great")
        self.assertFalse(self.partner.whatsmeow_marketing_optout)
        self.assertFalse(self.contact.optout)

    def test_stop_is_honoured_on_a_rejected_message(self):
        """Someone asking to be left alone is the last person whose message
        should be dropped unread."""
        self.session.inbound_default = "reject"
        self._inbound(body="/stop")
        self.assertFalse(self.env["whatsmeow.message"].search(
            [("wa_message_id", "=", "WAIN1")]))
        self.assertTrue(self.partner.whatsmeow_marketing_optout)

    def test_placeholder_cannot_opt_anyone_out_but_the_real_copy_can(self):
        self._inbound(body="", placeholder=True)
        self.assertFalse(self.partner.whatsmeow_marketing_optout)
        self._inbound(body="/stop")  # same wa_message_id: the real copy
        self.assertTrue(self.partner.whatsmeow_marketing_optout)

    def test_marketing_stop_does_not_block_transactional_messages(self):
        """'/stop' means no more promotions, not 'never message me again'."""
        self._inbound(body="/stop")
        message = self.env["whatsmeow.message"].create({
            "session_id": self.session.id, "direction": "out",
            "phone": "447700900001", "body": "Your order shipped",
        })
        self.assertFalse(message._optout_block_reason())

    def test_late_stop_blocks_an_already_queued_campaign_message(self):
        campaign = self._campaign(
            broadcast_list_ids=[(6, 0, self._list("L", self.contact).ids)])
        campaign.action_send()
        message = campaign.trace_ids.message_id
        self._inbound(body="/stop")
        with self._gateway(), \
                mute_logger("odoo.addons.whatsmeow.models.whatsmeow_message"):
            message.with_context(whatsmeow_queued=True).action_send()
        self.assertEqual(message.state, "error")
        self.assertIn("opted out", message.error_message)

    def test_a_stop_between_batches_is_never_queued_at_all(self):
        contacts = self.contact | self._contact("Omar", "447700900002")
        campaign = self._campaign(
            broadcast_list_ids=[(6, 0, self._list("L", contacts).ids)])
        campaign.write({"state": "sending"})
        campaign._resolve_recipients()
        self._inbound(body="/stop")
        campaign._materialise()
        stopped = campaign.trace_ids.filtered(lambda t: t.contact_id == self.contact)
        self.assertEqual(stopped.state, "skipped")
        self.assertEqual(stopped.skip_reason, "optout")
        self.assertFalse(stopped.message_id)

    def test_absolute_optout_rule_still_available(self):
        """A client who wants '/stop' to mean everything adds a core rule and
        gets exactly that, with no code."""
        self.env["whatsmeow.session.rule"].create({
            "session_id": self.session.id, "action": "optout", "keyword": "/stop",
        })
        self._inbound(body="/stop")
        self.assertTrue(self.partner.whatsmeow_optout)
        self.assertTrue(self.partner.whatsmeow_marketing_optout)


@tagged("post_install", "-at_install")
class TestReplies(MarketingCommon):

    def setUp(self):
        super().setUp()
        self.partner = self._partner("Sara", "447700900001")
        self.campaign = self._campaign(
            recipient_source="domain",
            mailing_domain=f"[('id', '=', {self.partner.id})]",
            body="Hello",
        )
        self.campaign.action_send()
        self._send_queued(self.campaign)
        self.trace = self.campaign.trace_ids

    def test_reply_is_credited_once(self):
        self._inbound(body="yes please")
        self.assertTrue(self.trace.replied)
        self.assertEqual(self.campaign.replied_count, 1)
        first = self.trace.reply_message_id
        self._inbound(wa_message_id="WAIN2", body="and one more thing")
        self.assertEqual(self.trace.reply_message_id, first,
                         "a conversation is one reply, not five")

    def test_reply_outside_the_window_is_not_credited(self):
        self.trace.message_id.sent_date = fields.Datetime.now() - timedelta(days=30)
        self._inbound(body="hello again")
        self.assertFalse(self.trace.replied)

    def test_reply_from_an_unknown_number_is_not_credited(self):
        self._inbound(sender_phone="447700909999",
                      chat_jid="447700909999@s.whatsapp.net", body="hi")
        self.assertFalse(self.trace.replied)


@tagged("post_install", "-at_install")
class TestDiscussRouting(MarketingCommon):

    def setUp(self):
        super().setUp()
        self.session.route_to_discuss = True
        self.session.route_fallback_user_ids = [(6, 0, self.other.ids)]
        self.partner = self._partner("Sara", "447700900001")
        self.campaign = self._campaign(
            recipient_source="domain",
            mailing_domain=f"[('id', '=', {self.partner.id})]",
            body="Hello",
            route_to_discuss=True,
            route_user_ids=[(6, 0, self.marketer.ids)],
        )
        self.campaign.action_send()
        self._send_queued(self.campaign)

    def _channel(self):
        return self.env["discuss.channel"].search([
            ("channel_type", "=", "whatsmeow"),
            ("whatsmeow_session_id", "=", self.session.id),
        ])

    def test_campaign_operators_attend_a_new_conversation(self):
        self._inbound(body="tell me more")
        channel = self._channel()
        self.assertEqual(len(channel), 1)
        self.assertIn(self.marketer.partner_id, channel.channel_member_ids.partner_id)
        self.assertNotIn(self.other.partner_id, channel.channel_member_ids.partner_id)

    def test_campaign_operators_are_added_to_an_existing_conversation(self):
        """Routing only runs on channel create, so a repeat customer's campaign
        reply would otherwise never reach the people running the campaign."""
        self.campaign.route_to_discuss = False
        self._inbound(body="an earlier, unrelated question")
        channel = self._channel()
        self.assertEqual(channel.channel_member_ids.partner_id, self.other.partner_id)
        self.campaign.route_to_discuss = True
        self._inbound(wa_message_id="WAIN2", body="about your offer")
        self.assertIn(self.marketer.partner_id, channel.channel_member_ids.partner_id)
        self.assertIn(self.other.partner_id, channel.channel_member_ids.partner_id,
                      "an operator already attending is never evicted")

    def test_unattributed_reply_uses_the_session_rules(self):
        self._inbound(sender_phone="447700909999",
                      chat_jid="447700909999@s.whatsapp.net", body="hi")
        channel = self._channel().filtered(
            lambda c: c.whatsmeow_chat_jid == "447700909999@s.whatsapp.net")
        self.assertEqual(channel.channel_member_ids.partner_id,
                         self.other.partner_id)


@tagged("post_install", "-at_install")
class TestBroadcastContact(MarketingCommon):

    def test_convert_links_an_existing_partner(self):
        partner = self._partner("Sara Ahmed", "+44 7788 123456")
        contact = self._contact("Sara", "447788123456")
        contact.action_convert_to_partner()
        self.assertEqual(contact.partner_id, partner,
                         "a second contact for the same number splits their history")

    def test_convert_creates_when_there_is_none(self):
        contact = self._contact("New Person", "447700900321")
        contact.action_convert_to_partner()
        self.assertTrue(contact.partner_id)
        self.assertEqual(contact.partner_id.phone, "447700900321")

    def test_convert_carries_the_optout_across(self):
        contact = self._contact("Gone", "447700900322", optout=True,
                                optout_reason="Replied '/stop' on WhatsApp")
        contact.action_convert_to_partner()
        self.assertTrue(contact.partner_id.whatsmeow_marketing_optout)

    def test_same_number_twice_for_one_owner_is_refused(self):
        self._contact("First", "+44 7700 900444")
        with self.assertRaises(Exception), mute_logger("odoo.sql_db"):
            with self.env.cr.savepoint():
                self._contact("Second", "447700900444")


@tagged("post_install", "-at_install")
class TestSecurity(MarketingCommon):

    def test_a_user_sees_only_their_own_campaigns(self):
        mine = self._campaign(user_id=self.marketer.id)
        theirs = self._campaign(user_id=self.other.id, name="Theirs")
        as_marketer = self.env(user=self.marketer)
        self.assertEqual(
            as_marketer["whatsmeow.marketing.campaign"].search(
                [("id", "in", (mine | theirs).ids)]),
            mine.with_env(as_marketer))
        with self.assertRaises(AccessError):
            theirs.with_env(as_marketer).read(["name"])

    def test_a_user_sees_only_their_own_broadcast_contacts(self):
        mine = self._contact("Mine", "447700900801", user_id=self.marketer.id)
        theirs = self._contact("Theirs", "447700900802", user_id=self.other.id)
        as_marketer = self.env(user=self.marketer)
        self.assertEqual(
            as_marketer["whatsmeow.broadcast.contact"].search(
                [("id", "in", (mine | theirs).ids)]),
            mine.with_env(as_marketer))

    def test_a_manager_sees_everything(self):
        manager = self.env["res.users"].create({
            "name": "Boss", "login": "boss",
            "group_ids": [(6, 0, [
                self.env.ref("base.group_user").id,
                self.env.ref(
                    "whatsmeow_marketing.group_whatsmeow_marketing_manager").id,
            ])],
        })
        mine = self._campaign(user_id=self.marketer.id)
        theirs = self._campaign(user_id=self.other.id, name="Theirs")
        as_manager = self.env(user=manager)
        self.assertEqual(
            as_manager["whatsmeow.marketing.campaign"].search(
                [("id", "in", (mine | theirs).ids)]),
            (mine | theirs).with_env(as_manager))

    def test_a_marketing_user_cannot_read_gateway_credentials(self):
        as_marketer = self.env(user=self.marketer)
        with self.assertRaises(AccessError):
            as_marketer["whatsmeow.connection"].browse(
                self.connection.id).read(["api_key"])

    def test_a_marketing_user_gets_no_whatsapp_app_but_keeps_its_own(self):
        """The connector's app is for administrators; a campaign runner works
        entirely from the Marketing app."""
        Menu = self.env["ir.ui.menu"]
        visible = Menu.with_user(self.marketer)._visible_menu_ids()
        self.assertNotIn(self.env.ref("whatsmeow.menu_whatsmeow_root").id, visible)
        self.assertIn(
            self.env.ref("whatsmeow_marketing.menu_whatsmeow_marketing_root").id,
            visible)

    def test_a_marketing_user_can_still_send(self):
        """Hiding the menu must not take the ability away: sending needs the
        ACLs the marketing group already implies, not a menu."""
        contact = self._contact("Sara", "447700900950", user_id=self.marketer.id)
        campaign = self._campaign(
            user_id=self.marketer.id,
            broadcast_list_ids=[(6, 0, self._list("L", contact).ids)],
        )
        as_marketer = campaign.with_user(self.marketer)
        as_marketer.action_send()
        self.assertEqual(as_marketer.state, "sending")
        message = as_marketer.trace_ids.message_id
        self.assertEqual(len(message), 1)
        with self._gateway():
            message.with_user(self.marketer).action_send()
        self.assertEqual(as_marketer.trace_ids.state, "sent")

    def test_a_marketing_user_can_send_a_test_message(self):
        """The test send is the one path that creates a message as the user
        rather than as the cron, so it is the one that proves their own ACLs."""
        contact = self._contact("Sara", "447700900951", user_id=self.marketer.id)
        campaign = self._campaign(
            user_id=self.marketer.id,
            broadcast_list_ids=[(6, 0, self._list("T", contact).ids)],
            test_phone="447700900999",
        )
        with self._gateway():
            campaign.with_user(self.marketer).action_send_test()
        sent = self.env["whatsmeow.message"].search([("phone", "=", "447700900999")])
        self.assertEqual(sent.state, "sent")
        self.assertFalse(sent.marketing_trace_id, "a test is not a recipient")

    def test_the_cron_ignores_who_owns_the_contacts(self):
        """Sending is a cron job: a record rule that hides a contact from
        whoever runs it must not silently shrink someone's audience."""
        contact = self._contact("Theirs", "447700900901", user_id=self.other.id)
        campaign = self._campaign(
            user_id=self.marketer.id,
            broadcast_list_ids=[(6, 0, self._list("L", contact).ids)])
        campaign.with_user(self.marketer).action_send()
        self.assertEqual(campaign.total_count, 1)


@tagged("post_install", "-at_install")
class TestBroadcastImport(MarketingCommon):
    """The spreadsheet importer. The point of these is the *edge* rows —
    a happy-path file is the easy half."""

    def _sheet(self, rows, headers=("Broadcast Name", "Name", "Phone"),
               sheet_name="Contacts"):
        book = Workbook()
        sheet = book.active
        sheet.title = sheet_name
        if headers:
            sheet.append(list(headers))
        for row in rows:
            sheet.append(list(row))
        buffer = BytesIO()
        book.save(buffer)
        return base64.b64encode(buffer.getvalue())

    def _wizard(self, rows, user=None, **vals):
        env = self.env(user=user) if user else self.env
        return env["whatsmeow.broadcast.import"].create({
            "file": self._sheet(rows) if isinstance(rows, list) else rows,
            "filename": "broadcast_import_template.xlsx",
            **vals,
        })

    def test_a_blank_broadcast_name_continues_the_list_above(self):
        wizard = self._wizard([
            ("Ramadan 2026", "One", "+919746701101"),
            ("", "Two", "+919746701102"),
            (None, "Three", "+919746701103"),
        ])
        wizard.action_import()
        self.assertEqual(wizard.state, "done")
        lists = wizard.imported_list_ids
        self.assertEqual(len(lists), 1, "one named row heads the whole block")
        self.assertEqual(lists.name, "Ramadan 2026")
        self.assertEqual(lists.contact_count, 3)

    def test_a_second_name_starts_a_second_list(self):
        wizard = self._wizard([
            ("List A", "One", "+919746707701"),
            ("", "Two", "+919746707702"),
            ("List B", "Three", "+919746707703"),
        ])
        wizard.action_import()
        by_name = {lst.name: lst for lst in wizard.imported_list_ids}
        self.assertEqual(sorted(by_name), ["List A", "List B"])
        self.assertEqual(by_name["List A"].contact_count, 2)
        self.assertEqual(by_name["List B"].contact_count, 1)

    def test_an_existing_number_is_reused_not_duplicated(self):
        existing = self._contact("Old Name", "+91 97467 01144")
        wizard = self._wizard([("New List", "New Name", "919746701144")])
        wizard.action_import()
        # Scoped to the owner: uniqueness is per owner, so a row someone else
        # holds for the same number is not this test's business.
        contacts = self.env["whatsmeow.broadcast.contact"].search(
            [("phone_tail", "=", "9746701144"), ("user_id", "=", self.env.uid)])
        self.assertEqual(contacts, existing, "the same person, one record")
        self.assertEqual(existing.name, "New Name")
        self.assertEqual(existing.list_ids, wizard.imported_list_ids)

    def test_update_names_off_keeps_the_stored_name(self):
        existing = self._contact("Old Name", "+91 97467 07755")
        wizard = self._wizard([("L", "New Name", "919746707755")],
                              update_names=False)
        wizard.action_import()
        self.assertEqual(existing.name, "Old Name")
        self.assertEqual(existing.list_ids, wizard.imported_list_ids,
                         "the subscription is added either way")

    def test_an_import_never_undoes_an_optout(self):
        gone = self._contact("Gone", "+91 97467 07766", optout=True,
                             optout_reason="Replied '/stop'")
        self._wizard([("L", "Gone", "919746707766")]).action_import()
        self.assertTrue(gone.optout, "re-uploading a list is not consent")

    def test_an_archived_contact_is_revived_rather_than_cloned(self):
        archived = self._contact("Sleeping", "+91 97467 07777")
        archived.active = False
        self._wizard([("L", "Sleeping", "919746707777")]).action_import()
        self.assertTrue(archived.active)
        self.assertEqual(
            self.env["whatsmeow.broadcast.contact"].with_context(
                active_test=False).search_count([("phone_tail", "=", "9746707777")]),
            1, "a second row would hide the first one's campaign history")

    def test_the_same_number_twice_in_one_file_is_reported_once(self):
        wizard = self._wizard([
            ("L", "First", "+919746707788"),
            ("", "Second", "919746707788"),
        ])
        wizard.action_import()
        self.assertIn("Row 3", wizard.skipped_summary)
        self.assertEqual(wizard.imported_list_ids.contact_count, 1)

    def test_an_unusable_number_is_skipped_with_its_row(self):
        wizard = self._wizard([
            ("L", "Fine", "+919746707799"),
            ("", "Broken", "N/A"),
            ("", "Empty", ""),
        ])
        wizard.action_import()
        self.assertEqual(wizard.imported_list_ids.contact_count, 1)
        self.assertIn("Row 3", wizard.skipped_summary)
        self.assertIn("Row 4", wizard.skipped_summary)

    def test_a_row_with_no_list_anywhere_is_skipped(self):
        wizard = self._wizard([("", "Nobody's", "+919746707811")])
        wizard.action_import()
        self.assertFalse(wizard.imported_list_ids)
        self.assertIn("Row 2", wizard.skipped_summary)

    def test_the_default_list_catches_an_unnamed_row(self):
        target = self.env["whatsmeow.broadcast.list"].create({"name": "Fallback"})
        wizard = self._wizard([("", "Somebody", "+919746707822")],
                              default_list_id=target.id)
        wizard.action_import()
        self.assertEqual(target.contact_count, 1)

    def test_a_number_typed_without_a_plus_survives_excel(self):
        # Excel stores it as a float; str() on that imports '9.19746707833e+11'.
        wizard = self._wizard([("L", "Numeric", 919746707833)])
        wizard.action_import()
        contact = self.env["whatsmeow.broadcast.contact"].search(
            [("phone_tail", "=", "9746707833")])
        self.assertEqual(contact.phone, "919746707833")

    def test_a_nameless_row_is_imported_under_its_number(self):
        wizard = self._wizard([("L", "", "+919746707844")])
        wizard.action_import()
        self.assertEqual(wizard.imported_list_ids.contact_ids.name, "+919746707844",
                         "a numbers-only list is a real thing to import")

    def test_an_unlabelled_sheet_falls_back_to_column_order(self):
        wizard = self._wizard(self._sheet(
            [("L", "Unlabelled", "+919746707855")], headers=None))
        wizard.action_import()
        self.assertEqual(wizard.imported_list_ids.name, "L")
        self.assertEqual(wizard.imported_list_ids.contact_count, 1)

    def test_an_existing_list_of_the_owner_is_reused_by_name(self):
        target = self.env["whatsmeow.broadcast.list"].create({"name": "Ramadan"})
        wizard = self._wizard([("ramadan", "Somebody", "+919746707866")])
        wizard.action_import()
        self.assertEqual(wizard.imported_list_ids, target,
                         "matching is case-insensitive, like the name a user types")

    def test_another_owners_list_of_the_same_name_is_not_touched(self):
        theirs = self.env["whatsmeow.broadcast.list"].create({
            "name": "Shared Name", "user_id": self.other.id,
        })
        wizard = self._wizard([("Shared Name", "Mine", "+919746707877")],
                              user=self.marketer)
        wizard.action_import()
        self.assertNotEqual(wizard.imported_list_ids, theirs)
        self.assertEqual(wizard.imported_list_ids.user_id, self.marketer)
        self.assertEqual(theirs.contact_count, 0)

    def test_a_marketing_user_can_run_the_whole_import(self):
        wizard = self._wizard([("Theirs", "Contact", "+919746707888")],
                              user=self.marketer)
        wizard.action_import()
        self.assertEqual(wizard.imported_list_ids.user_id, self.marketer)
        self.assertEqual(wizard.imported_list_ids.contact_ids.user_id, self.marketer)

    def test_a_non_excel_file_is_refused(self):
        wizard = self.env["whatsmeow.broadcast.import"].create({
            "file": base64.b64encode(b"name,phone\nX,1"), "filename": "list.csv",
        })
        with self.assertRaises(UserError):
            wizard.action_import()

    def test_a_corrupt_workbook_is_refused(self):
        wizard = self.env["whatsmeow.broadcast.import"].create({
            "file": base64.b64encode(b"not a workbook"), "filename": "list.xlsx",
        })
        with self.assertRaises(UserError):
            wizard.action_import()

    def test_the_shipped_template_imports(self):
        # The file the wizard hands out must be one the wizard accepts — the
        # two drift apart the moment nothing checks.
        path = file_path("whatsmeow_marketing/static/xlx/broadcast_import_template.xlsx")
        with open(path, "rb") as handle:
            wizard = self._wizard(base64.b64encode(handle.read()))
        wizard.action_import()
        self.assertEqual(wizard.imported_list_ids.name, "Broadcast list1")
        # Whether the sample numbers are new here or already on file, all three
        # must end up on the list — that is what the sheet asks for.
        self.assertEqual(wizard.imported_list_ids.contact_count, 3)
        self.assertFalse(wizard.skipped_summary, "the sample rows are all valid")
