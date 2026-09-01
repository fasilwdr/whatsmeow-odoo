import logging

from markupsafe import Markup

from odoo import _, fields, models
from odoo.exceptions import AccessError

_logger = logging.getLogger(__name__)


class WhatsmeowMessage(models.Model):
    """Remember which record a templated message was composed from, and log it
    on that record's chatter.

    Core posts *inbound* messages to the correspondent's chatter. This is the
    outbound counterpart: an operator who WhatsApps an invoice from its form
    expects the conversation to be part of that invoice's history, exactly as a
    sent email is.
    """
    _inherit = "whatsmeow.message"

    source_res_model = fields.Char(
        string="Source Model", readonly=True, index=True,
        help="Model of the record this message was composed from.",
    )
    source_res_id = fields.Many2oneReference(
        string="Source Record", model_field="source_res_model", readonly=True,
        index=True,
    )
    mail_message_id = fields.Many2one(
        "mail.message", string="Chatter Log", readonly=True, ondelete="set null",
        copy=False, help="The chatter entry logging this send on the source record.",
    )

    def _source_record(self):
        """The record this was composed from, or an empty recordset."""
        self.ensure_one()
        if not self.source_res_model or not self.source_res_id:
            return self.env["whatsmeow.message"].browse()
        if self.source_res_model not in self.env:
            return self.env["whatsmeow.message"].browse()
        return self.env[self.source_res_model].browse(self.source_res_id).exists()

    def _log_on_source(self):
        """Post an outbound message onto its source record's chatter.

        Logged as a *note* (`mail.mt_note`): the recipient already has the
        message on WhatsApp, and a subtype that notifies followers would email
        them a second copy of it.
        """
        for rec in self:
            record = rec._source_record()
            # Not every model is mail.thread — the composer can target any of them.
            if not record or not hasattr(record, "message_post"):
                continue
            # An operator who may *read* a record may log what they sent about
            # it. mail.message's create rule is stricter than that — it wants
            # write access — which would stop a salesperson WhatsApping a
            # contact they can only read. Check read explicitly, then post as
            # sudo with the real user as author, so the chatter still shows who
            # sent it.
            try:
                record.check_access_rights("read")
                record.check_access_rule("read")
            except AccessError:
                _logger.info(
                    "whatsmeow.message %s: no read access to %s(%s), not logged",
                    rec.id, rec.source_res_model, rec.source_res_id,
                )
                continue
            attachments = rec._chatter_attachments()
            message = record.sudo().message_post(
                # Markup(...) % value escapes the interpolated body. It is
                # operator-authored here rather than inbound, but the same
                # discipline costs nothing and survives a future caller.
                body=rec._chatter_body(),
                message_type="whatsmeow",
                subtype_xmlid="mail.mt_note",
                author_id=self.env.user.partner_id.id,
                attachment_ids=attachments.ids,
            )
            rec.mail_message_id = message.id

    def _chatter_body(self):
        """The body as it should read in the chatter: the message text, with
        WhatsApp's line breaks preserved."""
        self.ensure_one()
        body = (self.body or "").strip()
        if not body:
            # A media-only send still deserves a trace of what went out.
            return Markup("<p>%s</p>") % _(
                "Sent %(kind)s: %(filename)s",
                kind=self.message_type, filename=self.media_filename or "",
            )
        parts = [Markup("%s") % line for line in body.split("\n")]
        return Markup("<p>%s</p>") % Markup("<br/>").join(parts)

    def _chatter_attachments(self):
        """Copy the sent media onto the chatter post, so the log shows exactly
        what the recipient got."""
        self.ensure_one()
        if not self.media_data:
            return self.env["ir.attachment"]
        return self.env["ir.attachment"].create({
            "name": self.media_filename or "whatsapp-media",
            "datas": self.media_data,
            "mimetype": self.media_mimetype or "application/octet-stream",
            "res_model": self._name,
            "res_id": self.id,
        })
