import json

from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tests import TransactionCase, tagged
from odoo.tools import mute_logger

from odoo.addons.whatsmeow_template.models.whatsmeow_template import (
    MARKUP_CHARS, ZERO_WIDTH_SPACE,
)


class TemplateCommon(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.connection = cls.env["whatsmeow.connection"].create({
            "name": "GW", "base_url": "http://127.0.0.1:8081",
            "api_key": "k", "webhook_secret": "s",
        })
        cls.session = cls.env["whatsmeow.session"].create({
            "name": "ACME", "code": "acme", "connection_id": cls.connection.id,
        })
        cls.partner_model = cls.env["ir.model"]._get("res.partner")
        cls.alice = cls.env["res.partner"].create({
            "name": "Alice", "phone": "+44 7700 900123",
        })
        cls.bob = cls.env["res.partner"].create({
            "name": "Bob", "phone": "+44 7700 900456",
        })
        cls.nobody = cls.env["res.partner"].create({"name": "Nobody"})
        cls.template = cls.env["whatsmeow.template"].create({
            "name": "Greeting",
            "model_id": cls.partner_model.id,
            "session_id": cls.session.id,
            "body": "Hello {{ object.name }}, welcome.",
        })

    def _composer(self, records, **vals):
        return self.env["whatsmeow.composer"].create({
            "res_model": records._name,
            "res_ids": json.dumps(records.ids),
            **vals,
        })

    def _messages(self):
        # Scoped to this test's session: the dev database carries real messages
        # from earlier runs, and an unscoped search picks them up too.
        return self.env["whatsmeow.message"].search([
            ("direction", "=", "out"), ("session_id", "=", self.session.id),
        ])


@tagged("post_install", "-at_install")
class TestRendering(TemplateCommon):

    def test_body_interpolates_from_the_record(self):
        rendered = self.template._render_body(self.alice.ids)
        self.assertEqual(rendered[self.alice.id], "Hello Alice, welcome.")

    def test_each_record_renders_its_own_body(self):
        rendered = self.template._render_body((self.alice + self.bob).ids)
        self.assertEqual(rendered[self.alice.id], "Hello Alice, welcome.")
        self.assertEqual(rendered[self.bob.id], "Hello Bob, welcome.")

    def test_render_model_follows_the_template_model(self):
        self.assertEqual(self.template.render_model, "res.partner")

    def test_sender_and_company_are_in_the_context(self):
        """`user` comes from the mixin, `company` is ours — both name the
        sender, which is what a customer-facing sign-off needs."""
        self.template.body = "{{ user.name }} at {{ company.name }}"
        rendered = self.template._render_body(self.alice.ids)
        self.assertEqual(
            rendered[self.alice.id],
            f"{self.env.user.name} at {self.env.company.name}",
        )

    def test_company_follows_the_senders_active_company(self):
        other = self.env["res.company"].create({"name": "Branch Co"})
        self.template.body = "{{ company.name }}"
        rendered = self.template.with_company(other)._render_body(self.alice.ids)
        self.assertEqual(rendered[self.alice.id], "Branch Co")


@tagged("post_install", "-at_install")
class TestMarkupEscaping(TemplateCommon):
    """A rendered *value* must not be able to smuggle WhatsApp formatting.

    `{{ object.name }}` returning 'SO_2024_07' would italicise '2024' on the
    recipient's phone; the values most likely to carry a stray marker are the
    ones derived from inbound WhatsApp text, which is already untrusted.
    """

    def _render(self, name):
        self.alice.name = name
        return self.template._render_body(self.alice.ids)[self.alice.id]

    def test_markers_in_a_value_are_neutralised(self):
        rendered = self._render("SO_2024_07")
        self.assertNotIn("_2024_", rendered, "the underscores still pair up")
        # The text itself is untouched — the guards are zero-width.
        self.assertEqual(rendered.replace(ZERO_WIDTH_SPACE, ""),
                         "Hello SO_2024_07, welcome.")

    def test_every_marker_character_is_guarded(self):
        for marker in MARKUP_CHARS:
            with self.subTest(marker=marker):
                rendered = self._render(f"a{marker}b{marker}c")
                self.assertIn(f"{ZERO_WIDTH_SPACE}{marker}{ZERO_WIDTH_SPACE}", rendered)

    def test_the_authors_own_markers_are_left_alone(self):
        """Formatting the author typed is formatting they asked for."""
        template = self.template.copy({
            "name": "Author markers", "body": "*Hello* {{ object.name }}",
        })
        self.assertEqual(
            template._render_body(self.alice.ids)[self.alice.id], "*Hello* Alice",
        )

    def test_a_clean_value_is_unchanged(self):
        self.assertEqual(self._render("Alice"), "Hello Alice, welcome.")

    def test_an_empty_value_renders_as_nothing(self):
        """Wrapping the expression must not change what an empty value does.

        Odoo 16's inline_template has no `||| default` form (that arrived
        later): a falsy value simply contributes nothing. Asserted against the
        *unrewritten* body rather than a literal, so the test pins "the rewrite
        changes nothing here" instead of re-encoding Odoo's own whitespace
        handling around the braces.
        """
        template = self.template.copy({
            "name": "With an empty value", "body": "Hi {{ object.comment }}!",
        })
        plain = template._render_template(
            template.body, template.model, self.alice.ids, engine="inline_template",
        )
        self.assertEqual(
            template._render_body(self.alice.ids)[self.alice.id],
            plain[self.alice.id],
        )
        self.assertEqual(plain[self.alice.id], "Hi !")

    def test_the_stored_body_keeps_its_plain_expressions(self):
        """The rewrite is render-time only.

        If it were stored, `_has_unsafe_expression` would start seeing a
        function call and template authors outside
        `mail.group_mail_template_editor` could no longer save the record.
        """
        self.assertEqual(self.template.body, "Hello {{ object.name }}, welcome.")
        self.assertFalse(self.template._has_unsafe_expression())


@tagged("post_install", "-at_install")
class TestPhoneField(TemplateCommon):

    def test_explicit_path_is_used(self):
        self.template.phone_field = "phone"
        self.assertEqual(self.template._resolve_phone(self.alice), "+44 7700 900123")

    def test_related_path_is_traversed(self):
        child = self.env["res.partner"].create({
            "name": "Child", "parent_id": self.alice.id,
        })
        self.template.phone_field = "parent_id.phone"
        self.assertEqual(self.template._resolve_phone(child), "+44 7700 900123")

    def test_unknown_field_is_rejected_at_config_time(self):
        """§11.6: a bad path must fail while editing, not mid-blast."""
        with self.assertRaises(ValidationError):
            self.template.phone_field = "no_such_field"

    def test_non_text_leaf_is_rejected(self):
        with self.assertRaises(ValidationError):
            self.template.phone_field = "parent_id"

    def test_traversing_a_non_relational_field_is_rejected(self):
        with self.assertRaises(ValidationError):
            self.template.phone_field = "name.phone"

    def test_fallback_probes_the_record_then_its_contact(self):
        """The chatter button opens on models nobody templated, so resolution
        has to work with no configured path."""
        self.assertEqual(
            self.env["whatsmeow.template"]._probe_phone(self.alice),
            "+44 7700 900123",
        )

    def test_record_without_a_number_resolves_to_empty(self):
        self.assertEqual(self.template._resolve_phone(self.nobody), "")


@tagged("post_install", "-at_install")
class TestComposer(TemplateCommon):

    def test_single_send_queues_one_message(self):
        composer = self._composer(self.alice, template_id=self.template.id)
        self.assertFalse(composer.batch_mode)
        self.assertEqual(composer.body, "Hello Alice, welcome.")
        self.assertEqual(composer.phone, "+44 7700 900123")

        composer.action_send()
        message = self._messages()
        self.assertEqual(len(message), 1)
        self.assertEqual(message.state, "outgoing", "templated sends stay on the paced queue")
        self.assertEqual(message.body, "Hello Alice, welcome.")
        self.assertEqual(message.phone, "447700900123", "stored as digits")
        self.assertEqual(message.session_id, self.session)
        self.assertEqual(message.partner_id, self.alice)

    def test_batch_send_renders_per_record(self):
        composer = self._composer(self.alice + self.bob, template_id=self.template.id)
        self.assertTrue(composer.batch_mode)
        composer.action_send()

        messages = self._messages()
        self.assertEqual(len(messages), 2)
        self.assertEqual(
            {m.body for m in messages},
            {"Hello Alice, welcome.", "Hello Bob, welcome."},
        )
        self.assertEqual(set(messages.mapped("state")), {"outgoing"})

    def test_idempotency_key_is_per_message(self):
        composer = self._composer(self.alice + self.bob, template_id=self.template.id)
        composer.action_send()
        keys = [m._idempotency_key() for m in self._messages()]
        self.assertEqual(len(set(keys)), 2)

    @mute_logger("odoo.addons.whatsmeow_template.wizard.whatsmeow_composer")
    def test_records_without_a_number_are_skipped_not_dropped(self):
        composer = self._composer(
            self.alice + self.nobody, template_id=self.template.id)
        result = composer.action_send()

        self.assertEqual(len(self._messages()), 1)
        self.assertEqual(result["params"]["type"], "warning")
        self.assertIn("skipped", result["params"]["message"])

    @mute_logger("odoo.addons.whatsmeow_template.wizard.whatsmeow_composer")
    def test_no_reachable_recipient_raises(self):
        composer = self._composer(self.nobody, template_id=self.template.id)
        with self.assertRaises(UserError):
            composer.action_send()

    def test_composer_works_without_a_template(self):
        """The chatter button opens a bare composer on any model."""
        composer = self._composer(self.alice, session_id=self.session.id)
        composer.body = "Freeform hello"
        composer.action_send()

        message = self._messages()
        self.assertEqual(message.body, "Freeform hello")
        self.assertEqual(message.phone, "447700900123")

    def test_session_defaults_from_the_template(self):
        composer = self._composer(self.alice, template_id=self.template.id)
        self.assertEqual(composer.session_id, self.session)

    def test_deleted_record_is_dropped_before_sending(self):
        composer = self._composer(self.alice + self.bob, template_id=self.template.id)
        self.bob.unlink()
        composer.action_send()
        self.assertEqual(len(self._messages()), 1)

    def test_attachment_becomes_a_media_message(self):
        attachment = self.env["ir.attachment"].create({
            "name": "flyer.png",
            "datas": b"aGVsbG8=",
            "mimetype": "image/png",
        })
        self.template.attachment_ids = attachment
        composer = self._composer(self.alice, template_id=self.template.id)
        composer.action_send()

        message = self._messages()
        self.assertEqual(len(message), 1)
        self.assertEqual(message.message_type, "image")
        self.assertEqual(message.media_filename, "flyer.png")
        self.assertEqual(message.body, "Hello Alice, welcome.", "the body is the caption")

    def test_attachment_without_a_template_still_gets_its_kind(self):
        """The chatter button opens the composer with no template at all; the
        kind must still come from the mimetype."""
        attachment = self.env["ir.attachment"].create({
            "name": "clip.mp4", "datas": b"aGVsbG8=", "mimetype": "video/mp4",
        })
        composer = self._composer(self.alice, session_id=self.session.id)
        composer.attachment_ids = attachment
        composer.action_send()
        self.assertEqual(self._messages().message_type, "video")


@tagged("post_install", "-at_install")
class TestChatterLog(TemplateCommon):
    """A send must land on the source record's chatter, like a sent email."""

    def _logs(self, record):
        return record.message_ids.filtered(
            lambda m: m.message_type == "whatsmeow")

    def test_send_is_logged_on_the_source_record(self):
        composer = self._composer(self.alice, template_id=self.template.id)
        composer.action_send()

        log = self._logs(self.alice)
        self.assertEqual(len(log), 1)
        self.assertIn("Hello Alice, welcome.", log.body)
        self.assertEqual(log.author_id, self.env.user.partner_id)

    def test_log_is_a_note_so_followers_are_not_emailed(self):
        """The recipient already has it on WhatsApp; a notifying subtype would
        send them a second copy by email."""
        composer = self._composer(self.alice, template_id=self.template.id)
        composer.action_send()
        self.assertEqual(
            self._logs(self.alice).subtype_id,
            self.env.ref("mail.mt_note"),
        )

    def test_message_links_back_to_its_chatter_entry(self):
        composer = self._composer(self.alice, template_id=self.template.id)
        composer.action_send()
        message = self._messages()
        self.assertEqual(message.source_res_model, "res.partner")
        self.assertEqual(message.source_res_id, self.alice.id)
        self.assertTrue(message.mail_message_id)

    def test_line_breaks_survive_into_the_chatter(self):
        self.template.body = "Dear {{ object.name }},\n\nThank you."
        composer = self._composer(self.alice, template_id=self.template.id)
        composer.action_send()
        self.assertIn("<br>", self._logs(self.alice).body)

    def test_batch_logs_each_record_on_its_own_chatter(self):
        composer = self._composer(self.alice + self.bob, template_id=self.template.id)
        composer.action_send()
        self.assertIn("Hello Alice", self._logs(self.alice).body)
        self.assertIn("Hello Bob", self._logs(self.bob).body)

    def test_media_is_attached_to_the_log(self):
        attachment = self.env["ir.attachment"].create({
            "name": "flyer.png", "datas": b"aGVsbG8=", "mimetype": "image/png",
        })
        self.template.attachment_ids = attachment
        composer = self._composer(self.alice, template_id=self.template.id)
        composer.action_send()
        self.assertEqual(
            self._logs(self.alice).attachment_ids.mapped("name"), ["flyer.png"])

    def test_model_without_a_chatter_is_skipped_not_crashed(self):
        """The composer can target any model; not all of them are mail.thread."""
        currency = self.env["res.currency"].search([], limit=1)
        message = self.env["whatsmeow.message"].create({
            "session_id": self.session.id, "direction": "out",
            "phone": "447700900123", "body": "hi",
            "source_res_model": currency._name, "source_res_id": currency.id,
        })
        message._log_on_source()
        self.assertFalse(message.mail_message_id)


@tagged("post_install", "-at_install")
class TestReportFilename(TemplateCommon):
    """The customer must receive the filename the operator sees in the UI."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.report = cls.env["ir.actions.report"].create({
            "name": "Partner Sheet",
            "model": "res.partner",
            "report_type": "qweb-pdf",
            "report_name": "whatsmeow_template.dummy_partner_report",
        })

    def test_print_report_name_is_used(self):
        self.report.print_report_name = "'Statement_%s' % object.name"
        name = self.template._report_filename(self.report, self.alice)
        self.assertEqual(name, "Statement_Alice.pdf")

    def test_extension_is_not_doubled(self):
        self.report.print_report_name = "'Statement.pdf'"
        self.assertEqual(
            self.template._report_filename(self.report, self.alice),
            "Statement.pdf",
        )

    def test_falls_back_to_the_record_name(self):
        """Without print_report_name, the record's name beats the technical
        report_name the old code used."""
        self.report.print_report_name = False
        self.assertEqual(
            self.template._report_filename(self.report, self.alice),
            "Alice.pdf",
        )

    def test_slashes_become_underscores_like_the_browser_download(self):
        """An invoice named INV/2026/00001 downloads as INV_2026_00001.pdf,
        because Content-Disposition escapes the slash and the browser rewrites
        it. Nothing does that on the WhatsApp path, so we do it here."""
        self.alice.name = "INV/2026/00001"
        self.report.print_report_name = "object.name"
        self.assertEqual(
            self.template._report_filename(self.report, self.alice),
            "INV_2026_00001.pdf",
        )

    def test_non_pdf_format_keeps_its_extension(self):
        self.report.print_report_name = "'Sheet_%s' % object.name"
        self.assertEqual(
            self.template._report_filename(self.report, self.alice, "html"),
            "Sheet_Alice.html",
        )


@tagged("post_install", "-at_install")
class TestServerAction(TemplateCommon):

    def test_running_on_n_records_queues_n_messages(self):
        action = self.env["ir.actions.server"].create({
            "name": "WhatsApp the customer",
            "model_id": self.partner_model.id,
            "state": "whatsmeow_send",
            "whatsmeow_template_id": self.template.id,
        })
        action.with_context(
            active_model="res.partner",
            active_ids=(self.alice + self.bob).ids,
            active_id=self.alice.id,
        ).run()

        messages = self._messages()
        self.assertEqual(len(messages), 2)
        self.assertEqual(set(messages.mapped("session_id").ids), {self.session.id})

    def test_template_must_match_the_action_model(self):
        with self.assertRaises(ValidationError):
            self.env["ir.actions.server"].create({
                "name": "Mismatched",
                "model_id": self.env["ir.model"]._get("res.users").id,
                "state": "whatsmeow_send",
                "whatsmeow_template_id": self.template.id,
            })

    def test_state_requires_a_template(self):
        with self.assertRaises(ValidationError):
            self.env["ir.actions.server"].create({
                "name": "No template",
                "model_id": self.partner_model.id,
                "state": "whatsmeow_send",
            })


@tagged("post_install", "-at_install")
class TestSidebarAction(TemplateCommon):

    def test_creating_a_template_binds_it_to_its_model(self):
        """This is what makes 'Send WhatsApp' appear in any model's Action menu."""
        action = self.template.ref_ir_act_window
        self.assertTrue(action)
        self.assertEqual(action.binding_model_id, self.partner_model)
        self.assertEqual(action.res_model, "whatsmeow.composer")
        self.assertIn(str(self.template.id), action.context)

    def test_changing_the_model_retargets_the_binding(self):
        users_model = self.env["ir.model"]._get("res.users")
        self.template.write({"model_id": users_model.id, "phone_field": False})
        self.assertEqual(self.template.ref_ir_act_window.binding_model_id, users_model)

    def test_deleting_a_template_removes_its_binding(self):
        action = self.template.ref_ir_act_window
        self.template.unlink()
        self.assertFalse(action.exists())

    def test_archiving_withdraws_the_binding(self):
        """An archived template must not stay in the Action menu: the composer
        would open on a template its own domain no longer offers."""
        action = self.template.ref_ir_act_window
        self.template.action_archive()
        self.assertFalse(action.exists())
        self.assertFalse(self.template.ref_ir_act_window)

    def test_activating_rebuilds_the_binding(self):
        self.template.action_archive()
        self.template.action_unarchive()
        self.assertTrue(self.template.active)
        self.assertEqual(
            self.template.ref_ir_act_window.binding_model_id, self.partner_model)

    def test_an_archived_template_is_created_without_a_binding(self):
        template = self.env["whatsmeow.template"].create({
            "name": "Draft", "model_id": self.partner_model.id,
            "body": "wip", "active": False,
        })
        self.assertFalse(template.ref_ir_act_window)


@tagged("post_install", "-at_install")
class TestAccess(TemplateCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.operator = cls.env["res.users"].create({
            "name": "Operator", "login": "wa_operator",
            "groups_id": [(6, 0, [
                cls.env.ref("base.group_user").id,
                cls.env.ref("whatsmeow.group_whatsmeow_user").id,
            ])],
        })

    def test_plain_user_can_send_via_the_composer(self):
        """§11.5: using a template is a user act; editing one is not."""
        composer = self.env["whatsmeow.composer"].with_user(self.operator).create({
            "res_model": "res.partner",
            "res_ids": json.dumps(self.alice.ids),
            "template_id": self.template.id,
        })
        composer.action_send()
        self.assertEqual(len(self._messages()), 1)

    def test_plain_user_cannot_edit_a_template(self):
        with self.assertRaises(AccessError):
            self.template.with_user(self.operator).write({"body": "hijacked"})

    def test_plain_user_cannot_create_a_template(self):
        with self.assertRaises(AccessError):
            self.env["whatsmeow.template"].with_user(self.operator).create({
                "name": "Sneaky",
                "model_id": self.partner_model.id,
                "body": "hi",
            })


@tagged("post_install", "-at_install")
class TestComposerOptOut(TemplateCommon):
    """The opt-out gate (core, PLAN.md §12.3) must hold on the composer path
    too — this is the test that stops a future module reintroducing a hole."""

    def test_a_batch_sends_to_everyone_but_the_opted_out(self):
        from unittest.mock import patch
        self.alice.whatsmeow_optout = True
        composer = self._composer(self.alice + self.bob, template_id=self.template.id)
        composer.action_send()
        messages = self._messages()
        self.assertEqual(len(messages), 2, "the rows are queued either way")

        with patch.object(type(self.session), "_gw",
                          return_value={"wa_message_id": "T1"}) as gw, \
                mute_logger("odoo.addons.whatsmeow.models.whatsmeow_message"):
            self.env["whatsmeow.message"].cron_process_outgoing()
        self.assertEqual(gw.call_count, 1, "only Bob's message reached the gateway")
        blocked = messages.filtered(lambda m: m.partner_id == self.alice)
        self.assertEqual(blocked.state, "error")
        self.assertIn("opted out", blocked.error_message)
        self.assertEqual(messages.filtered(lambda m: m.partner_id == self.bob).state,
                         "sent")
