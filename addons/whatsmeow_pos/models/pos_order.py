import re

from odoo import models
from odoo.exceptions import UserError
from odoo.tools import BinaryBytes

DIGITS = re.compile(r"\D")


class PosOrder(models.Model):
    _inherit = "pos.order"

    def action_send_receipt_whatsmeow(self, phone):
        """Send this order's receipt to `phone` over WhatsApp — and its invoice,
        when the order was invoiced. The WhatsApp twin of `action_send_receipt`.

        Sent there and then rather than queued: the customer is standing at the
        counter waiting for it, and one receipt is a person's pace of sending,
        not a burst. Everything else a send goes through — the opt-out gate,
        the chatter log, the delivery receipts — is the core message's own.

        Returns `{"error": ...}` instead of raising when the gateway refuses, so
        the messages stay on record (and can be resent from WhatsApp >
        Messages) rather than vanishing in a rollback.
        """
        self.ensure_one()
        config = self.config_id
        if not config.whatsmeow_enabled:
            raise UserError(self.env._(
                "WhatsApp receipts are turned off for %s.", config.name))
        digits = self._whatsmeow_digits(phone)
        if not digits:
            raise UserError(self.env._("%s is not a phone number.", phone or "''"))
        session = config._whatsmeow_pos_session()
        if session.status != "connected":
            raise UserError(self.env._(
                "No WhatsApp number is connected for %s.", config.name))

        self.mobile = phone
        # sudo: a cashier need not be a WhatsApp user to hand over a receipt.
        # The chatter still names them, as `_log_on_source` posts with the real
        # user as author.
        Message = self.env["whatsmeow.message"].sudo()
        partner = Message._find_partner(digits)
        target = {"session_id": session.id, "phone": digits, "partner_id": partner.id}

        vals_list = self._whatsmeow_receipt_vals(target)
        if self.account_move:
            vals_list += self._whatsmeow_invoice_vals(target)
        messages = Message.create(vals_list)
        messages._log_on_source()
        messages.action_send()
        failed = messages.filtered(lambda m: m.state == "error")
        if failed:
            return {"error": "\n".join(dict.fromkeys(failed.mapped("error_message")))}
        return {"sent": len(messages)}

    def _whatsmeow_digits(self, phone):
        """The number as WhatsApp wants it: digits with the country code.

        Formatted against the customer's country (or the shop's) first, so a
        local number typed at the till — 050 123 4567 — still finds the phone.
        """
        country = self.partner_id.country_id or self.company_id.country_id
        formatted = self._phone_format(number=phone, country=country) if phone else False
        return DIGITS.sub("", formatted or phone or "")

    def _whatsmeow_template(self, chosen, xmlid):
        """The point of sale's own template, else the default shipped here —
        or an empty recordset if someone deleted that one too. sudo: the
        cashier reads templates only to send them."""
        template = chosen or self.env.ref(xmlid, raise_if_not_found=False)
        return (template or self.env["whatsmeow.template"]).sudo()

    def _whatsmeow_receipt_vals(self, target):
        template = self._whatsmeow_template(
            self.config_id.whatsmeow_receipt_template_id, "whatsmeow_pos.template_pos_receipt")
        media = [("Receipt-%s.jpg" % self.name, "image/jpeg",
                  BinaryBytes(self.order_receipt_generate_image()))]
        return self._whatsmeow_vals(self, template, target, media)

    def _whatsmeow_invoice_vals(self, target):
        move = self.account_move
        template = self._whatsmeow_template(
            self.config_id.whatsmeow_invoice_template_id, "whatsmeow_pos.template_pos_invoice")
        rendered = template._render_report(move) if template else None
        if not rendered:
            # A template with no report of its own still owes the customer the
            # invoice itself — the same PDF the emailed receipt carries.
            report = self.env.ref("account.account_invoices")
            content = self.env["ir.actions.report"]._render_qweb_pdf(report, move.ids)[0]
            rendered = (self.env["whatsmeow.template"]._report_filename(report, move), content)
        media = [(rendered[0], "application/pdf", BinaryBytes(rendered[1]))]
        return self._whatsmeow_vals(move, template, target, media)

    def _whatsmeow_vals(self, record, template, target, media):
        """The message(s) one record produces: the first file carries the text,
        any further file follows bare — a message holds a single file."""
        body = template._render_body(record.ids).get(record.id, "") if template else ""
        media = media + [
            (att.name, att.mimetype or "application/octet-stream", att.raw)
            for att in template.attachment_ids
        ]
        Message = self.env["whatsmeow.message"]
        common = {
            **target,
            "direction": "out",
            "source_res_model": record._name,
            "source_res_id": record.id,
        }
        return [{
            **common,
            "message_type": Message._kind_for_mimetype(mimetype),
            "body": body if index == 0 else "",
            "media_data": data,
            "media_filename": name,
            "media_mimetype": mimetype,
        } for index, (name, mimetype, data) in enumerate(media)]
