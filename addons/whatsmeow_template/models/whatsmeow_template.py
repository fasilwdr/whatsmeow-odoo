import logging

from odoo import api, fields, models
from odoo.exceptions import UserError, ValidationError
# `time` here is safe_eval's *wrapped* module: Odoo 19 refuses a raw module in
# an evaluation context, and this is the same object the report download route
# and mail.template hand to print_report_name.
from odoo.tools.osutil import clean_filename
from odoo.tools.safe_eval import safe_eval, time

from odoo.addons.whatsmeow.models.whatsmeow_message import (
    MESSAGE_TYPES,
    PHONE_FIELDS,
)

_logger = logging.getLogger(__name__)

# Field types a phone path may end on. A number is text; anything else (a
# many2one, a float, a binary) is a configuration mistake we can catch now
# rather than at send time, when a blast is already half out the door.
PHONE_LEAF_TYPES = ("char", "text")

# Field names probed when a template sets no explicit `phone_field`, in order.
# `PHONE_FIELDS` is core's own probe list (Odoo 19 dropped res.partner.mobile,
# but a localisation may add it back), reused here so the fallback and
# `_find_partner` never disagree about what counts as a phone column.
PHONE_FALLBACK_PATHS = (
    *PHONE_FIELDS,
    *(f"partner_id.{name}" for name in PHONE_FIELDS),
)


class WhatsmeowTemplate(models.Model):
    """A saved WhatsApp message, rendered against a record of any model.

    Meta's Cloud API forces pre-registered templates, positional {{1}}
    variables and a 24-hour session window; none of that exists on the
    unofficial Web protocol, so a template here is just a body plus optional
    media, rendered and dropped on the core outgoing queue. See PLAN.md §11.1.
    """
    _name = "whatsmeow.template"
    _description = "Whatsmeow Template"
    _inherit = ["mail.render.mixin"]
    _order = "sequence, name"

    name = fields.Char(required=True, translate=True)
    active = fields.Boolean(default=True)
    sequence = fields.Integer(default=10)
    model_id = fields.Many2one(
        "ir.model", string="Applies to", required=True, ondelete="cascade",
        help="The model whose records this template can be sent from.",
    )
    model = fields.Char(related="model_id.model", store=True, readonly=True)
    session_id = fields.Many2one(
        "whatsmeow.session", string="Send From",
        help="Default WhatsApp number to send from. The composer can override it.",
    )
    phone_field = fields.Char(
        string="Recipient Field",
        help="Field path on the model holding the recipient's number, e.g. "
             "'phone' or 'partner_id.phone'. Leave empty to probe the record's "
             "own phone fields and then its contact's.",
    )
    body = fields.Text(
        required=True,
        help="The message. Placeholders are interpolated against the record: "
             "{{ object.name }}, {{ object.partner_id.name }}, {{ user.name }}.",
    )
    message_type = fields.Selection(
        MESSAGE_TYPES, default="text", required=True,
        help="Anything other than Text sends the attachment (or the rendered "
             "report) as media, with the body as its caption.",
    )
    attachment_ids = fields.Many2many(
        "ir.attachment", string="Attachments",
        help="Static files sent with every message from this template.",
    )
    report_id = fields.Many2one(
        "ir.actions.report", string="Report",
        help="Rendered as a PDF for each record and attached to the message.",
    )
    # mail.template's own pattern for a per-model Action-menu entry.
    ref_ir_act_window = fields.Many2one(
        "ir.actions.act_window", string="Sidebar Action", readonly=True, copy=False,
        help="Action-menu entry making this template available on records of "
             "its model.",
    )

    _name_model_uniq = models.Constraint(
        "UNIQUE (name, model_id)",
        "A template with this name already exists for that model.",
    )

    @api.depends("model")
    def _compute_render_model(self):
        """mail.render.mixin renders against this. It leaves the model blank and
        expects each inheriting model to say where records come from."""
        for tmpl in self:
            tmpl.render_model = tmpl.model

    # -- configuration validation --------------------------------------------
    @api.constrains("model_id", "phone_field")
    def _check_phone_field(self):
        """Resolve the path now, so a typo surfaces while editing the template
        rather than silently dropping every recipient of a batch send."""
        for tmpl in self.filtered("phone_field"):
            error = tmpl._phone_field_error(tmpl.phone_field)
            if error:
                raise ValidationError(error)

    @api.constrains("model_id", "report_id")
    def _check_report_model(self):
        for tmpl in self.filtered("report_id"):
            if tmpl.report_id.model != tmpl.model:
                raise ValidationError(self.env._(
                    "Report '%(report)s' is for model %(report_model)s, but this "
                    "template applies to %(model)s.",
                    report=tmpl.report_id.name, report_model=tmpl.report_id.model,
                    model=tmpl.model,
                ))

    def _phone_field_error(self, path):
        """Validate a dotted field path against the template's model.

        Traversal is restricted to many2one hops ending on a text field: that
        keeps a path a *field* lookup and never an arbitrary attribute walk,
        which is the same discipline as rendering through the mixin instead of
        eval().
        """
        self.ensure_one()
        model = self.env.get(self.model)
        if model is None:
            return self.env._("Model %s is not installed.", self.model)
        parts = [p.strip() for p in (path or "").split(".") if p.strip()]
        if not parts:
            return self.env._("The recipient field path is empty.")
        for index, part in enumerate(parts):
            field = model._fields.get(part)
            if field is None:
                return self.env._(
                    "Field '%(field)s' does not exist on %(model)s.",
                    field=part, model=model._name,
                )
            last = index == len(parts) - 1
            if last:
                if field.type not in PHONE_LEAF_TYPES:
                    return self.env._(
                        "Field '%(field)s' on %(model)s is a %(type)s; a recipient "
                        "number must be a text field.",
                        field=part, model=model._name, type=field.type,
                    )
            elif field.type != "many2one":
                return self.env._(
                    "Cannot follow '%(field)s' on %(model)s: only many2one fields "
                    "can be traversed in a recipient path.",
                    field=part, model=model._name,
                )
            else:
                model = self.env[field.comodel_name]
        return None

    # -- rendering ------------------------------------------------------------
    def _render_body(self, res_ids):
        """{record id: rendered text}.

        `inline_template` is the engine behind {{ object.field }} — Odoo 19
        offers inline_template/qweb/qweb_view, and it is the one mail.template
        uses for its subject. It is sandboxed, so a template author cannot
        reach beyond the record.
        """
        self.ensure_one()
        return self._render_template(
            self.body or "", self.model, list(res_ids), engine="inline_template",
        )

    def _render_report(self, record):
        """Render `report_id` for one record as (filename, bytes), or None.

        The name comes from the report's `print_report_name` — the same
        expression the Print menu evaluates in `web`'s report download route
        and that `mail.template` uses for emailed reports. Anything else and
        the customer receives 'account.report_invoice_with_payments-42.pdf'
        where the operator saw 'INV_2026_00001.pdf'.
        """
        self.ensure_one()
        report = self.report_id
        if not report:
            return None
        content, report_format = self.env["ir.actions.report"] \
            .with_context(active_model=self.model) \
            ._render_qweb_pdf(report, [record.id])
        return self._report_filename(report, record, report_format), content

    @api.model
    def _report_filename(self, report, record, report_format="pdf"):
        name = None
        if report.print_report_name:
            # Trusted config, evaluated in the same sandbox and with the same
            # variables as the download route: only `object` and `time`.
            name = safe_eval(report.print_report_name,
                             {"object": record, "time": time})
        if not name:
            name = record.display_name or report.name
        # The download route hands the raw name to Content-Disposition, which
        # percent-encodes '/' and leaves the *browser* to turn it into '_' —
        # which is why an operator sees INV_2026_00001.pdf for a record named
        # INV/2026/00001. Nothing does that for us here, so do it explicitly:
        # the filename must read the same in WhatsApp as it did in the UI.
        name = clean_filename(str(name).strip(), replacement="_")
        extension = f".{report_format or 'pdf'}"
        return name if name.endswith(extension) else name + extension

    def _resolve_phone(self, record):
        """The recipient number for one record, or '' when it has none.

        An explicit `phone_field` wins; otherwise probe the record's own phone
        columns and then its contact's, which is what makes the chatter button
        work on a model nobody has configured a template for.
        """
        self.ensure_one()
        if self.phone_field:
            return self._traverse(record, self.phone_field)
        return self._probe_phone(record)

    @api.model
    def _probe_phone(self, record):
        """Best-effort number for a record with no configured path.

        Model-level on purpose: the chatter button opens the composer on models
        nobody has templated, and that path needs this answer without a
        template record to ask.
        """
        for path in PHONE_FALLBACK_PATHS:
            value = self._traverse(record, path)
            if value:
                return value
        return ""

    @api.model
    def _traverse(self, record, path):
        """Follow a dotted field path on a record, tolerating gaps.

        Only used on paths already validated by `_phone_field_error` (explicit)
        or hardcoded (the fallbacks), so a missing field here means the model
        simply does not have it — not an injection attempt.
        """
        value = record
        for part in [p.strip() for p in (path or "").split(".") if p.strip()]:
            if not value or part not in value._fields:
                return ""
            value = value[part]
            if not value:
                return ""
        return value if isinstance(value, str) else ""

    # -- Action-menu binding (mail.template's create_action pattern) ----------
    def create_action(self):
        """Publish this template in its model's contextual Action menu.

        Odoo builds that menu from `ir.actions.act_window` records bound to a
        model, so one binding per template is how a template reaches records of
        *any* model without touching that model's views.
        """
        view = self.env.ref("whatsmeow_template.whatsmeow_composer_view_form")
        for tmpl in self:
            if tmpl.ref_ir_act_window:
                continue
            action = self.env["ir.actions.act_window"].sudo().create({
                "name": self.env._("Send WhatsApp (%s)", tmpl.name),
                "type": "ir.actions.act_window",
                "res_model": "whatsmeow.composer",
                "context": repr({"default_template_id": tmpl.id}),
                "view_mode": "form",
                "view_id": view.id,
                "target": "new",
                "binding_model_id": tmpl.model_id.id,
                "binding_type": "action",
            })
            tmpl.ref_ir_act_window = action.id
        return True

    def unlink_action(self):
        self.mapped("ref_ir_act_window").sudo().unlink()
        return True

    @api.model_create_multi
    def create(self, vals_list):
        templates = super().create(vals_list)
        # On by default: a template you cannot launch from a record is useless,
        # and mail.template's opt-in button is a relic of a busier sidebar.
        templates.create_action()
        return templates

    def write(self, vals):
        res = super().write(vals)
        if "model_id" in vals:
            # The binding names a model; retarget it rather than leaving the
            # entry on the old model's records, where it would now fail.
            for tmpl in self.filtered("ref_ir_act_window"):
                tmpl.ref_ir_act_window.sudo().binding_model_id = tmpl.model_id.id
        if "name" in vals:
            for tmpl in self.filtered("ref_ir_act_window"):
                tmpl.ref_ir_act_window.sudo().name = self.env._(
                    "Send WhatsApp (%s)", tmpl.name)
        return res

    def unlink(self):
        self.unlink_action()
        return super().unlink()

    # -- UI -------------------------------------------------------------------
    def action_open_composer(self):
        """Open the composer preloaded with this template, for a test send."""
        self.ensure_one()
        record = self.env[self.model].search([], limit=1)
        if not record:
            raise UserError(self.env._(
                "There is no %s record to preview this template against.",
                self.model_id.name,
            ))
        return {
            "type": "ir.actions.act_window",
            "name": self.env._("Send WhatsApp"),
            "res_model": "whatsmeow.composer",
            "view_mode": "form",
            "views": [(self.env.ref(
                "whatsmeow_template.whatsmeow_composer_view_form").id, "form")],
            "target": "new",
            "context": {
                "default_template_id": self.id,
                "active_model": self.model,
                "active_id": record.id,
                "active_ids": record.ids,
            },
        }
