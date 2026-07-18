import json

from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tests import TransactionCase, tagged
from odoo.tools import mute_logger


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


@tagged("post_install", "-at_install")
class TestAccess(TemplateCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.operator = cls.env["res.users"].create({
            "name": "Operator", "login": "wa_operator",
            "group_ids": [(6, 0, [
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
