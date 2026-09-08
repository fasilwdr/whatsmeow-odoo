from unittest.mock import patch

from odoo.exceptions import UserError
from odoo.tests import TransactionCase, tagged
from odoo.tools import mute_logger

from odoo.addons.whatsmeow.controllers.webhook import WhatsmeowWebhook


class BotCommon(TransactionCase):
    """A bot, a number, and a way to put a message in front of it.

    The gateway is never reached: `action_send` is patched to mark the message
    sent, so a test exercises the engine rather than HTTP. What matters here is
    what the bot decided to say and store, and every one of those decisions is a
    record.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.connection = cls.env["whatsmeow.connection"].create({
            "name": "GW", "base_url": "http://127.0.0.1:8080",
            "api_key": "k", "webhook_secret": "s",
        })
        cls.session = cls.env["whatsmeow.session"].create({
            "name": "ACME", "code": "acme", "connection_id": cls.connection.id,
            "bot_enabled": True,
        })
        cls.operator = cls.env["res.users"].create({
            "name": "Operator", "login": "bot_operator",
            "groups_id": [(6, 0, [cls.env.ref("base.group_user").id])],
        })
        cls.ctrl = WhatsmeowWebhook()

    def setUp(self):
        super().setUp()
        # Every send is answered as the gateway would have: state 'sent', with a
        # WhatsApp id. Patching `action_send` rather than the HTTP layer keeps
        # the queue's own rules (opt-out, validation) in play.
        def fake_send(messages):
            for message in messages:
                if message.direction == "out" and message.state == "outgoing":
                    message.write({"state": "sent", "wa_message_id": f"OUT{message.id}"})

        # Patched on the *registry* class, not on the module's own: another
        # bridge may sit above it in the MRO, and a patch it shadows is a patch
        # that silently does nothing.
        patcher = patch.object(
            type(self.env["whatsmeow.message"]), "action_send",
            autospec=True, side_effect=fake_send)
        patcher.start()
        self.addCleanup(patcher.stop)
        self._wa_seq = 0

    # -- helpers --------------------------------------------------------------
    def _bot(self, **vals):
        values = {
            "name": "Front desk",
            "chat_type": "private",
            "timeout_minutes": 0,
        }
        values.update(vals)
        return self.env["whatsmeow.bot"].create(values)

    def _step(self, bot, sequence, step_type, **vals):
        values = {"bot_id": bot.id, "sequence": sequence, "step_type": step_type}
        values.update(vals)
        return self.env["whatsmeow.bot.step"].create(values)

    def _inbound(self, body="hello", wa_id=None, **over):
        self._wa_seq += 1
        payload = {
            "wa_message_id": wa_id or f"WA{self._wa_seq}",
            "sender_phone": "447700900001",
            "sender_jid": "447700900001@s.whatsapp.net",
            "chat_jid": "447700900001@s.whatsapp.net",
            "body": body,
        }
        payload.update(over)
        with mute_logger("odoo.addons.whatsmeow.controllers.webhook"):
            self.ctrl._on_message(self.env, self.session, payload)

    def _chats(self):
        return self.env["whatsmeow.bot.chat"].search([
            ("session_id", "=", self.session.id)])

    def _sent_bodies(self, chat):
        return chat.line_ids.filtered(lambda l: l.direction == "out").mapped("body")


@tagged("post_install", "-at_install")
class TestBotEngine(BotCommon):

    def test_a_bot_with_no_steps_never_speaks(self):
        self._bot()
        self._inbound(wa_id="W1")
        self.assertFalse(self._chats(), "an empty workflow has nothing to say")

    def test_greeting_then_question_is_one_turn(self):
        """A message and the question after it go out together — the contact
        cannot answer a question they have not been asked."""
        bot = self._bot()
        self._step(bot, 10, "message", body="Hi!")
        self._step(bot, 20, "question", body="Your name?", store_key="who")
        self._inbound(wa_id="W1")

        chat = self._chats()
        self.assertEqual(len(chat), 1)
        self.assertEqual(self._sent_bodies(chat), ["Hi!", "Your name?"])
        self.assertEqual(chat.current_step_id.store_key, "who")
        self.assertEqual(chat.state, "running")

    def test_an_answer_is_stored_and_the_flow_moves_on(self):
        bot = self._bot()
        self._step(bot, 10, "question", body="Your name?", store_key="who")
        self._step(bot, 20, "end", body="Thanks {{ answer.who }}!")
        self._inbound(wa_id="W1")
        self._inbound(body="Ada", wa_id="W2")

        chat = self._chats()
        self.assertEqual(chat.value_ids.mapped("value"), ["Ada"])
        # The placeholder is resolved against what was collected a moment ago,
        # which is the whole point of storing it before rendering the next step.
        self.assertIn("Thanks Ada!", self._sent_bodies(chat))
        self.assertEqual(chat.state, "done")

    def test_options_are_numbered_and_pick_their_branch(self):
        bot = self._bot()
        menu = self._step(bot, 10, "choice", body="How can I help?",
                          store_key="topic")
        sales = self._step(bot, 20, "end", body="Sales it is.")
        support = self._step(bot, 30, "end", body="Support it is.")
        self.env["whatsmeow.bot.answer"].create([
            {"step_id": menu.id, "sequence": 10, "name": "Sales",
             "next_step_id": sales.id},
            {"step_id": menu.id, "sequence": 20, "name": "Support",
             "next_step_id": support.id},
        ])
        self._inbound(wa_id="W1")

        chat = self._chats()
        self.assertIn("1. Sales", self._sent_bodies(chat)[0])
        self.assertIn("2. Support", self._sent_bodies(chat)[0])

        self._inbound(body="2", wa_id="W2")
        self.assertIn("Support it is.", self._sent_bodies(chat))
        self.assertEqual(chat.value_ids.mapped("value"), ["Support"])

    def test_an_option_can_be_answered_in_words(self):
        bot = self._bot()
        menu = self._step(bot, 10, "choice", body="Yes or no?", store_key="ok")
        self.env["whatsmeow.bot.answer"].create({
            "step_id": menu.id, "name": "Yes", "keywords": "y, yeah, ok",
        })
        self._inbound(wa_id="W1")
        self._inbound(body="Yeah", wa_id="W2")
        self.assertEqual(self._chats().value_ids.mapped("value"), ["Yes"])

    def test_a_bad_answer_is_asked_again_then_given_up_on(self):
        bot = self._bot(max_retries=2, on_max_retry="end",
                        invalid_message="Try again")
        self._step(bot, 10, "question", body="Email?", input_type="email",
                   store_key="email")
        self._inbound(wa_id="W1")
        self._inbound(body="not an email", wa_id="W2")

        chat = self._chats()
        self.assertIn("Try again", self._sent_bodies(chat))
        self.assertEqual(chat.state, "running")

        self._inbound(body="still not", wa_id="W3")
        self.assertEqual(chat.state, "done", "the second failure spends the last try")
        self.assertFalse(chat.value_ids, "nothing readable was ever collected")

    def test_a_good_answer_clears_the_retry_count(self):
        bot = self._bot(max_retries=2, on_max_retry="end")
        self._step(bot, 10, "question", body="Email?", input_type="email",
                   store_key="email")
        self._step(bot, 20, "question", body="Phone?", input_type="phone",
                   store_key="phone")
        self._inbound(wa_id="W1")
        self._inbound(body="nope", wa_id="W2")
        self._inbound(body="ada@example.com", wa_id="W3")

        chat = self._chats()
        self.assertEqual(chat.retry_count, 0)
        self.assertEqual(chat.current_step_id.store_key, "phone")

    def test_stop_and_restart_words_work_from_anywhere(self):
        bot = self._bot(cancel_keywords="stop", cancel_message="Bye",
                        restart_keywords="menu")
        self._step(bot, 10, "question", body="Your name?", store_key="who")
        self._step(bot, 20, "question", body="Your email?", store_key="email")
        self._inbound(wa_id="W1")
        self._inbound(body="Ada", wa_id="W2")

        chat = self._chats()
        self.assertEqual(chat.value_ids.mapped("key"), ["who"])

        self._inbound(body="menu", wa_id="W3")
        self.assertEqual(chat.state, "running")
        self.assertFalse(chat.value_ids, "a restart forgets what was collected")
        self.assertEqual(chat.current_step_id.store_key, "who")

        self._inbound(body="stop", wa_id="W4")
        self.assertEqual(chat.state, "cancelled")
        self.assertIn("Bye", self._sent_bodies(chat))

    def test_a_loop_is_stopped_rather_than_sent(self):
        """Two message steps pointing at each other must not send until the
        number is banned."""
        bot = self._bot()
        first = self._step(bot, 10, "message", body="ping")
        second = self._step(bot, 20, "message", body="pong", next_step_id=first.id)
        first.next_step_id = second.id
        with mute_logger("odoo.addons.whatsmeow_bot.models.whatsmeow_bot_chat"):
            self._inbound(wa_id="W1")
        chat = self._chats()
        self.assertEqual(chat.state, "done")
        self.assertLessEqual(len(self._sent_bodies(chat)), 50)

    def test_expired_chats_start_over(self):
        bot = self._bot(timeout_minutes=1)
        self._step(bot, 10, "question", body="Your name?", store_key="who")
        self._inbound(wa_id="W1")
        chat = self._chats()
        chat.last_activity = "2000-01-01 00:00:00"

        self._inbound(body="Ada", wa_id="W2")
        self.assertEqual(chat.state, "expired")
        self.assertEqual(len(self._chats()), 2, "a new chat picks up the reply")


@tagged("post_install", "-at_install")
class TestBotMatching(BotCommon):

    def test_criteria_decide_which_bot_answers(self):
        quiet = self._bot(name="Invoices", sequence=10, keyword="invoice")
        self._step(quiet, 10, "end", body="Invoice bot")
        general = self._bot(name="General", sequence=20)
        self._step(general, 10, "end", body="General bot")

        self._inbound(body="about my invoice", wa_id="W1")
        self.assertIn("Invoice bot", self._sent_bodies(self._chats()))

    def test_a_number_with_bots_off_is_left_alone(self):
        self.session.bot_enabled = False
        bot = self._bot()
        self._step(bot, 10, "end", body="hi")
        self._inbound(wa_id="W1")
        self.assertFalse(self._chats())

    def test_a_bot_pinned_to_another_number_stays_quiet(self):
        other = self.env["whatsmeow.session"].create({
            "name": "Other", "code": "other", "connection_id": self.connection.id,
        })
        bot = self._bot(session_ids=[(6, 0, other.ids)])
        self._step(bot, 10, "end", body="hi")
        self._inbound(wa_id="W1")
        self.assertFalse(self._chats())

    def test_the_bot_does_not_talk_over_an_operator(self):
        """A conversation an operator is attending is theirs."""
        bot = self._bot()
        self._step(bot, 10, "end", body="hi")
        channel = self.env["mail.channel"].create({
            "name": "Chat", "channel_type": "whatsmeow",
            "whatsmeow_session_id": self.session.id,
            "whatsmeow_chat_jid": "447700900001@s.whatsapp.net",
        })
        channel.channel_member_ids.unlink()
        channel.add_members(partner_ids=self.operator.partner_id.ids,
                            post_joined_message=False)
        self._inbound(wa_id="W1")
        self.assertFalse(self._chats())


@tagged("post_install", "-at_install")
class TestBotHandover(BotCommon):

    def test_handover_opens_a_conversation_and_holds_the_bot_off(self):
        bot = self._bot(handover_user_ids=[(6, 0, self.operator.ids)],
                        handover_message="Passing you over",
                        # The point under test is the handover hold itself, so
                        # the "never interrupt a human" guard is taken out of the
                        # way — otherwise the channel's own membership would be
                        # what keeps the bot quiet after the release.
                        skip_if_attended=False)
        self._step(bot, 10, "question", body="Your name?", store_key="who")
        self._step(bot, 20, "handover")
        self._inbound(wa_id="W1")
        self._inbound(body="Ada", wa_id="W2")

        chat = self._chats()
        self.assertEqual(chat.state, "handover")
        self.assertTrue(chat.channel_id, "the conversation now exists in Discuss")
        self.assertIn(self.operator.partner_id,
                      chat.channel_id.channel_member_ids.partner_id)
        # The briefing is a note, so it can never be relayed back to the contact.
        briefing = chat.channel_id.message_ids.filtered(
            lambda m: m.message_type == "notification")
        self.assertTrue(any("Ada" in (m.body or "") for m in briefing))

        # And the bot stays out until someone releases it.
        self._inbound(body="hello again", wa_id="W3")
        self.assertEqual(len(self._chats()), 1)

        chat.action_release()
        self._inbound(body="hello again", wa_id="W4")
        self.assertEqual(len(self._chats()), 2)


@tagged("post_install", "-at_install")
class TestBotServerActions(BotCommon):

    def test_a_server_action_sees_what_was_collected(self):
        action = self.env["ir.actions.server"].create({
            "name": "Tag the chat",
            "model_id": self.env.ref("whatsmeow_bot.model_whatsmeow_bot_chat").id,
            "state": "code",
            "code": "record.write({'push_name': env.context.get('whatsmeow_bot_values', {}).get('who', '')})",
        })
        bot = self._bot()
        self._step(bot, 10, "question", body="Your name?", store_key="who",
                   server_action_ids=[(6, 0, action.ids)])
        self._inbound(wa_id="W1")
        self._inbound(body="Ada", wa_id="W2")
        self.assertEqual(self._chats().push_name, "Ada")

    def test_a_failing_action_does_not_end_the_chat(self):
        action = self.env["ir.actions.server"].create({
            "name": "Boom",
            "model_id": self.env.ref("whatsmeow_bot.model_whatsmeow_bot_chat").id,
            "state": "code",
            "code": "raise ValueError('boom')",
        })
        bot = self._bot()
        self._step(bot, 10, "message", body="Hi",
                   server_action_ids=[(6, 0, action.ids)])
        self._step(bot, 20, "end", body="Bye")
        with mute_logger("odoo.addons.whatsmeow_bot.models.whatsmeow_bot_chat"):
            self._inbound(wa_id="W1")
        chat = self._chats()
        self.assertIn("Bye", self._sent_bodies(chat))
        self.assertTrue(chat.line_ids.filtered(
            lambda l: l.direction == "note" and "failed" in (l.body or "")))


@tagged("post_install", "-at_install")
class TestBotBuilder(BotCommon):

    def test_the_canvas_reads_the_links_it_has_to_draw(self):
        bot = self._bot()
        menu = self._step(bot, 10, "choice", body="Pick", store_key="topic")
        bye = self._step(bot, 20, "end", body="Thanks")
        self.env["whatsmeow.bot.answer"].create({
            "step_id": menu.id, "name": "Sales", "next_step_id": bye.id,
        })
        data = self.env["whatsmeow.bot"].builder_data(bot.id)

        self.assertEqual([step["id"] for step in data["steps"]], [menu.id, bye.id])
        answer = data["steps"][0]["answers"][0]
        self.assertEqual(answer["next_step_id"], bye.id)
        self.assertEqual(answer["next_step_title"], bye.display_name)

    def test_a_drag_renumbers_the_flow(self):
        bot = self._bot()
        first = self._step(bot, 10, "message", body="One")
        second = self._step(bot, 20, "message", body="Two")

        self.env["whatsmeow.bot"].builder_reorder(bot.id, [second.id, first.id])
        self.assertEqual(bot._sorted_steps().ids, [second.id, first.id])
        # Order is the flow: what was second now runs first, and falls through
        # into what used to be above it.
        self.assertEqual(second._following_step(), first)

    def test_steps_of_another_bot_are_refused(self):
        bot = self._bot()
        other = self._bot(name="Other")
        stranger = self._step(other, 10, "message", body="Not yours")
        with self.assertRaises(UserError):
            self.env["whatsmeow.bot"].builder_reorder(bot.id, [stranger.id])

    def test_multiple_actions_all_run(self):
        """A step can fire several server actions, and one that raises does not
        cancel the others."""
        Action = self.env["ir.actions.server"]
        model = self.env.ref("whatsmeow_bot.model_whatsmeow_bot_chat").id
        boom = Action.create({
            "name": "Boom", "model_id": model, "state": "code",
            "code": "raise ValueError('boom')",
        })
        tag = Action.create({
            "name": "Tag", "model_id": model, "state": "code",
            "code": "record.write({'push_name': 'tagged'})",
        })
        bot = self._bot()
        self._step(bot, 10, "message", body="Hi",
                   server_action_ids=[(6, 0, (boom | tag).ids)])
        self._step(bot, 20, "end", body="Bye")
        with mute_logger("odoo.addons.whatsmeow_bot.models.whatsmeow_bot_chat"):
            self._inbound(wa_id="W1")
        self.assertEqual(self._chats().push_name, "tagged")
