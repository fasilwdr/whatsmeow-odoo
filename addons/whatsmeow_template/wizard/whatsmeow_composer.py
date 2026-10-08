import base64
import json
import logging
import re

from odoo import api, fields, models
from odoo.exceptions import UserError, ValidationError

_logger = logging.getLogger(__name__)

DIGITS = re.compile(r"\D")


class WhatsmeowComposer(models.TransientModel):
    """Compose a WhatsApp message from one or many records of any model.

    Deliberately thin: it renders, resolves a number and creates outgoing
    `whatsmeow.message` rows. Everything that makes a send safe — per-session
    pacing, retries, the idempotency key — already lives in core's queue, so
    this feature adds no transport code at all (PLAN.md §11.3).
    """
    _name = "whatsmeow.composer"
    _description = "Whatsmeow Composer"

    res_model = fields.Char(
        string="Model", required=True, default=lambda self: self.env.context.get("active_model"),
    )
    res_ids = fields.Char(
        string="Record IDs", required=True, default=lambda self: self._default_res_ids(),
    )
    record_count = fields.Integer(compute="_compute_record_count")
    batch_mode = fields.Boolean(compute="_compute_record_count")

    template_id = fields.Many2one(
        "whatsmeow.template", string="Template",
        domain="[('model', '=', res_model)]",
    )
    # Not `required=True`: Odoo inserts the row and *then* computes stored
    # computed fields, so a required one dies on the NOT NULL column before its
    # compute ever runs. The constraint below enforces it at the same moment
    # for the user, without constraining the write order.
    session_id = fields.Many2one(
        "whatsmeow.session", string="Send From",
        compute="_compute_session_id", store=True, readonly=False,
    )
    phone = fields.Char(
        string="Recipient",
        compute="_compute_single_values", store=True, readonly=False,
        help="Resolved from the record. Only meaningful for a single send.",
    )
    group_ids = fields.Many2many(
        "whatsmeow.group", string="Groups",
        compute="_compute_group_ids", store=True, readonly=False,
        help="WhatsApp groups that also receive the message, taken from the "
             "template. Each is messaged from the number that belongs to it, "
             "whatever Send From says.",
    )
    body = fields.Text(
        string="Message",
        compute="_compute_single_values", store=True, readonly=False,
        help="Rendered from the template against the record. Edit before sending.",
    )
    attachment_ids = fields.Many2many("ir.attachment", string="Attachments")

    @api.model
    def _default_res_ids(self):
        context = self.env.context
        ids = context.get("active_ids") or (
            [context["active_id"]] if context.get("active_id") else []
        )
        return json.dumps(list(ids))

    def _records(self):
        """The records this composer targets, as a recordset."""
        self.ensure_one()
        if not self.res_model or self.res_model not in self.env:
            return self.env["whatsmeow.message"].browse()
        try:
            ids = json.loads(self.res_ids or "[]")
        except ValueError:
            ids = []
        # exists() drops anything deleted between opening and sending the wizard.
        return self.env[self.res_model].browse(ids).exists()

    @api.depends("res_model", "res_ids")
    def _compute_record_count(self):
        for comp in self:
            count = len(comp._records())
            comp.record_count = count
            comp.batch_mode = count > 1

    @api.depends("template_id")
    def _compute_session_id(self):
        """Deliberately never reads `self.session_id`.

        Reading the field being computed inside its own compute leaves it unset
        on create — the record then hits the NOT NULL column and the insert
        fails. An explicit value passed to create() still wins over this, which
        is what preserves an operator's override.
        """
        # A single-number install should never have to pick one.
        fallback = self.env["whatsmeow.session"].search([], limit=1)
        for comp in self:
            comp.session_id = comp.template_id.session_id or fallback

    @api.depends("template_id")
    def _compute_group_ids(self):
        for comp in self:
            comp.group_ids = comp.template_id.group_ids

    @api.constrains("session_id")
    def _check_session(self):
        for comp in self:
            if not comp.session_id:
                raise ValidationError(comp.env._(
                    "Choose which WhatsApp number to send from."
                ))

    @api.depends("template_id", "res_model", "res_ids")
    def _compute_single_values(self):
        """Preview text and recipient — single mode only.

        In batch mode each record renders its own body and resolves its own
        number at send time, so a single editable preview would be a lie.
        """
        for comp in self:
            record = comp._records()
            if len(record) != 1:
                comp.phone = False
                comp.body = False
                continue
            template = comp.template_id
            # Never read comp.body/comp.phone here: reading a field inside its
            # own compute does not give back what the operator typed, it gives
            # back whatever the ORM is mid-way through computing.
            if template:
                comp.body = template._render_body(record.ids).get(record.id, "")
                # The template's own groups, not comp.group_ids: that field is
                # computed from the same template, and reading one compute's
                # output from another makes the result depend on their order.
                comp.phone = template._resolve_phone(record) \
                    if template._sends_to_number(template.group_ids) else False
            else:
                comp.body = False
                comp.phone = comp._fallback_phone(record)

    def _fallback_phone(self, record):
        """Resolve a number with no template configured — the case behind the
        chatter button on a model nobody has templated yet."""
        self.ensure_one()
        return self.env["whatsmeow.template"]._probe_phone(record)

    def _record_phone(self, record):
        """The number one record's message goes to, "" when it goes to none."""
        self.ensure_one()
        template = self.template_id
        if not template:
            return self._fallback_phone(record)
        if not template._sends_to_number(self.group_ids):
            return ""  # a group-only template: see `_sends_to_number`
        return template._resolve_phone(record)

    # -- sending --------------------------------------------------------------
    def action_send(self):
        """Queue one message per record and recipient. Nothing is sent inline.

        A record's message goes to its number, to each group, or to both —
        whatever the template (and the operator, who may edit either) named.

        The mirror image of the Discuss bridge's live reply: a template send is
        exactly the bursty many-recipient traffic the per-session throttle
        exists to smooth, so these rows go on the paced queue and
        `cron_process_outgoing` places them (PLAN.md §11.3).
        """
        self.ensure_one()
        records = self._records()
        if not records:
            raise UserError(self.env._("There is nothing to send this message to."))

        template = self.template_id
        bodies = template._render_body(records.ids) if template else {}
        # The operator's edited preview only speaks for a single record. It can
        # go stale — a record deleted between opening and sending turns a batch
        # into a single send whose preview was never computed — so use it only
        # when it actually holds something.
        use_preview = len(records) == 1 and bool(self.phone or self.body)

        groups = self.group_ids
        vals_list, skipped, numberless = [], [], []
        for record in records:
            if use_preview:
                phone = self.phone
                body = self.body or ""
            else:
                phone = self._record_phone(record)
                body = bodies.get(record.id, self.body or "")
            digits = DIGITS.sub("", phone or "")
            if not digits and not groups:
                skipped.append(record.display_name)
                continue
            # Rendered once per record, not once per recipient: a report is the
            # expensive part, and every copy of this record's message carries
            # the same file.
            media = self._media_items(record)
            if digits:
                vals_list.extend(self._message_vals(record, digits, body, media=media))
            elif self._wants_number():
                numberless.append(record.display_name)
            for group in groups:
                vals_list.extend(
                    self._message_vals(record, "", body, group=group, media=media))

        if skipped and not vals_list:
            raise UserError(self.env._(
                "None of the selected records have a WhatsApp number:\n%s",
                "\n".join(skipped[:10]),
            ))
        if numberless:
            # The groups still got it, so the record is not skipped — but the
            # private copy it was meant to have did not go out.
            _logger.info(
                "whatsmeow.composer: %s record(s) reached their groups but had "
                "no number of their own: %s",
                len(numberless), ", ".join(numberless[:10]),
            )

        messages = self.env["whatsmeow.message"].create(vals_list)
        # Logged now, at queue time, not when the gateway confirms: the chatter
        # records what the operator did, and the queue may not place the message
        # for another minute. Delivery state lives on the whatsmeow.message row.
        messages._log_on_source()
        if skipped:
            # Reported, never silently dropped: a batch that quietly reaches 40
            # of 50 customers is worse than one that says so.
            _logger.info(
                "whatsmeow.composer: %s record(s) had no number: %s",
                len(skipped), ", ".join(skipped[:10]),
            )
        return self._done_action(len(messages), skipped)

    def _wants_number(self):
        """Whether this send is meant to reach each record's own number."""
        self.ensure_one()
        template = self.template_id
        return template._sends_to_number(self.group_ids) if template else True

    def _target_vals(self, digits, group):
        """Where one copy of the message goes: a number, or a group.

        A group copy leaves from the group's own session rather than the
        composer's: only the number that is a member can post there, so the
        operator's Send From cannot apply to it.
        """
        self.ensure_one()
        if group:
            return {"session_id": group.session_id.id, "group_id": group.id}
        partner = self.env["whatsmeow.message"]._find_partner(digits)
        return {
            "session_id": self.session_id.id,
            "phone": digits,
            "partner_id": partner.id,
        }

    def _message_vals(self, record, digits, body, group=None, media=None):
        """Values for the message(s) one record produces for one recipient.

        A `whatsmeow.message` carries at most one file, so extra attachments
        become their own follow-up messages — the queue paces them apart just
        like any other send.

        Each file's kind comes from its mimetype, the same derivation the
        gateway's `kindFor()` and inbound media use — one rule for what a file
        is, wherever it entered Odoo.
        """
        self.ensure_one()
        common = {
            **self._target_vals(digits, group),
            "direction": "out",
            # Where this was composed from, so the send can be logged on that
            # record's chatter the way a sent email is.
            "source_res_model": record._name,
            "source_res_id": record.id,
        }
        if media is None:
            media = self._media_items(record)
        if not media:
            return [{**common, "message_type": "text", "body": body}]

        vals_list = []
        for index, (name, mimetype, data) in enumerate(media):
            vals_list.append({
                **common,
                "message_type": self.env["whatsmeow.message"]._kind_for_mimetype(mimetype),
                # Only the first file carries the message; the rest follow bare.
                "body": body if index == 0 else "",
                "media_data": data,
                "media_filename": name,
                "media_mimetype": mimetype,
                "media_state": "none",
            })
        return vals_list

    def _media_items(self, record):
        """(filename, mimetype, base64 bytes) for everything to attach."""
        self.ensure_one()
        items = []
        attachments = self.attachment_ids | self.template_id.attachment_ids
        for attachment in attachments:
            items.append((
                attachment.name,
                attachment.mimetype or "application/octet-stream",
                attachment.datas,
            ))
        rendered = self.template_id._render_report(record) if self.template_id else None
        if rendered:
            name, content = rendered
            items.append((name, "application/pdf", base64.b64encode(content)))
        return items

    def _done_action(self, sent, skipped):
        self.ensure_one()
        if skipped:
            message = self.env._(
                "%(sent)s message(s) queued. %(skipped)s record(s) had no WhatsApp "
                "number and were skipped.", sent=sent, skipped=len(skipped),
            )
        else:
            message = self.env._("%s message(s) queued.", sent)
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "message": message,
                "type": "warning" if skipped else "success",
                "next": {"type": "ir.actions.act_window_close"},
            },
        }
