import base64
import logging
import mimetypes
import re

from markupsafe import Markup

from odoo import api, fields, models
from odoo.exceptions import ValidationError
from odoo.tools import SQL

from .whatsmeow_connection import MEDIA_TIMEOUT

_logger = logging.getLogger(__name__)
DIGITS = re.compile(r"\D")

# Odoo 19's res.partner has no `mobile` field (it was merged into `phone`), but
# localisation/OCA modules may add one back. Probe the model instead of assuming.
PHONE_FIELDS = ("phone", "mobile")


class WhatsmeowMessage(models.Model):
    _name = "whatsmeow.message"
    _description = "Whatsmeow Message"
    _order = "create_date desc, id desc"
    _rec_name = "body"

    session_id = fields.Many2one(
        "whatsmeow.session", required=True, ondelete="cascade", index=True,
    )
    connection_id = fields.Many2one(
        related="session_id.connection_id", store=True, index=True,
    )
    partner_id = fields.Many2one("res.partner", index=True)
    phone = fields.Char(
        help="Digits with country code, e.g. 447700900123. Empty on an inbound "
             "message whose sender is only known by LID.",
    )
    sender_lid = fields.Char(
        string="Sender LID", readonly=True, index=True,
        help="WhatsApp's privacy-preserving id for the sender. WhatsApp does not "
             "always reveal the phone number behind it, so this may be the only "
             "identity we have.",
    )
    direction = fields.Selection(
        [("out", "Outgoing"), ("in", "Incoming")], required=True, default="out",
    )
    body = fields.Text(string="Message / Caption")
    message_type = fields.Selection(
        [
            ("text", "Text"),
            ("image", "Image"),
            ("video", "Video"),
            ("audio", "Audio"),
            ("document", "Document"),
            ("sticker", "Sticker"),
        ],
        default="text", required=True,
    )
    # attachment=True keeps the bytes in the filestore instead of the database.
    media_data = fields.Binary(string="Media", attachment=True)
    media_filename = fields.Char(string="Filename")
    media_mimetype = fields.Char(string="MIME Type")
    media_size = fields.Integer(string="Size (bytes)", readonly=True)
    media_state = fields.Selection(
        [
            ("none", "No Media"),
            ("pending", "Waiting for Download"),
            ("fetched", "Downloaded"),
            ("error", "Download Failed"),
        ],
        default="none", required=True, readonly=True,
        help="Inbound media is downloaded from the gateway by a scheduled action, "
             "so a large file cannot stall the webhook.",
    )
    is_voice_note = fields.Boolean(
        readonly=True, help="Audio recorded in WhatsApp rather than an attached file.",
    )
    media_duration = fields.Integer(
        string="Duration (s)", readonly=True, help="For audio and video.",
    )
    wa_message_id = fields.Char(readonly=True, index=True)
    state = fields.Selection(
        [
            ("outgoing", "Queued"),
            ("sent", "Sent"),
            ("delivered", "Delivered"),
            ("read", "Read"),
            ("received", "Received"),
            ("error", "Error"),
        ],
        default="outgoing", required=True,
    )
    error_message = fields.Char(readonly=True)

    @api.constrains("direction", "phone")
    def _check_phone_for_outgoing(self):
        # Inbound may legitimately have no phone (LID-only sender); outbound
        # cannot be sent anywhere without one.
        for rec in self:
            if rec.direction == "out" and not (rec.phone or "").strip():
                raise ValidationError(self.env._(
                    "A phone number is required to send a WhatsApp message."
                ))

    @api.constrains("direction", "message_type", "body", "media_data")
    def _check_content_for_outgoing(self):
        # Inbound records are created by the webhook before the media is fetched,
        # so only outgoing messages are required to be complete.
        for rec in self.filtered(lambda r: r.direction == "out"):
            if rec.message_type == "text" and not (rec.body or "").strip():
                raise ValidationError(self.env._("A text message needs a body."))
            if rec.message_type != "text" and not rec.media_data:
                raise ValidationError(self.env._(
                    "A %s message needs a file attached.", rec.message_type,
                ))

    @api.onchange("media_filename")
    def _onchange_media_filename(self):
        """Guess the type from the file the user picked, so they rarely have to
        set it by hand."""
        if not self.media_filename:
            return
        mimetype = mimetypes.guess_type(self.media_filename)[0] or ""
        self.media_mimetype = mimetype
        self.message_type = self._kind_for_mimetype(mimetype)

    @api.model
    def _kind_for_mimetype(self, mimetype):
        """Mirrors kindFor() in the gateway: webp is how WhatsApp does stickers."""
        mimetype = (mimetype or "").lower()
        if mimetype.startswith("image/webp"):
            return "sticker"
        if mimetype.startswith("image/"):
            return "image"
        if mimetype.startswith("video/"):
            return "video"
        if mimetype.startswith("audio/"):
            return "audio"
        return "document"

    @api.model
    def _find_partner(self, phone):
        """Match a partner on the last 10 digits of their number.

        A plain `('phone', 'like', tail)` domain cannot do this: stored numbers
        are formatted ('+44 7700 900123'), so LIKE '%7700900123%' never matches.
        Strip non-digits in SQL and compare the tails instead.
        """
        digits = DIGITS.sub("", phone or "")
        if not digits:
            return self.env["res.partner"]
        tail = digits[-10:]
        partner = self.env["res.partner"]
        columns = [f for f in PHONE_FIELDS if f in partner._fields]
        if not columns:
            return partner

        condition = SQL(" OR ").join(
            SQL(
                "regexp_replace(COALESCE(%s, ''), '\\D', '', 'g') LIKE %s",
                SQL.identifier("res_partner", col), f"%{tail}",
            )
            for col in columns
        )
        self.env.cr.execute(SQL(
            "SELECT id FROM res_partner WHERE active AND (%s) ORDER BY id LIMIT 1",
            condition,
        ))
        row = self.env.cr.fetchone()
        return partner.browse(row[0]) if row else partner

    def _send_payload(self):
        """Build the gateway call for this message: text and media use
        different endpoints and payloads."""
        self.ensure_one()
        code = self.session_id.code
        if self.message_type == "text":
            return f"/sessions/{code}/send", {
                "phone": self.phone, "message": self.body or "",
            }
        return f"/sessions/{code}/send-media", {
            "phone": self.phone,
            "caption": self.body or "",
            "filename": self.media_filename or "",
            "mimetype": self.media_mimetype or "",
            "kind": self.message_type,
            "ptt": self.is_voice_note,
            # media_data is already base64 in Odoo; decode/re-encode would be waste.
            "data": (self.media_data or b"").decode(),
        }

    def action_send(self):
        for rec in self.filtered(
            lambda r: r.direction == "out" and r.state in ("outgoing", "error")
        ):
            try:
                path, payload = rec._send_payload()
                # Uploading a file takes longer than posting a line of text.
                timeout = None if rec.message_type == "text" else MEDIA_TIMEOUT
                data = rec.session_id._gw("POST", path, payload, timeout=timeout)
                rec.write({
                    "state": "sent",
                    "wa_message_id": data.get("wa_message_id"),
                    "error_message": False,
                })
            except Exception as exc:  # noqa: BLE001 - one bad send must not stop the queue
                rec.write({"state": "error", "error_message": str(exc)})
                _logger.warning("whatsmeow.message %s send failed: %s", rec.id, exc)

    @api.model
    def cron_process_outgoing(self):
        # Small batches + gaps between runs = lower ban risk.
        self.search(
            [("direction", "=", "out"), ("state", "=", "outgoing")],
            limit=10, order="create_date asc",
        ).action_send()

    # -- inbound media --------------------------------------------------------
    def action_fetch_media(self):
        """Pull the bytes the gateway downloaded for us, then let it drop them.

        Deliberately not done inline in the webhook: a large file would hold the
        webhook (and its transaction) open past the gateway's timeout, which
        would just make it retry.
        """
        for rec in self.filtered(lambda r: r.media_state == "pending"):
            code = rec.session_id.code
            try:
                content, headers = rec.session_id.connection_id._request_raw(
                    "GET", f"/sessions/{code}/media/{rec.wa_message_id}")
            except Exception as exc:  # noqa: BLE001 - keep the batch moving
                rec.write({"media_state": "error", "error_message": str(exc)})
                _logger.warning("whatsmeow.message %s media fetch failed: %s", rec.id, exc)
                continue

            rec.write({
                "media_data": base64.b64encode(content),
                "media_size": len(content),
                "media_state": "fetched",
                "error_message": False,
            })
            rec._post_to_chatter()
            # Best-effort: the gateway garbage-collects anything we miss.
            try:
                rec.session_id._gw("DELETE", f"/sessions/{code}/media/{rec.wa_message_id}")
            except Exception as exc:  # noqa: BLE001
                _logger.info("whatsmeow: could not release media %s: %s", rec.wa_message_id, exc)

    def _post_to_chatter(self):
        """Post an inbound message onto the partner's chatter, with the media
        attached when there is any."""
        self.ensure_one()
        if not self.partner_id:
            return
        attachments = self.env["ir.attachment"]
        if self.media_data:
            attachments = self.env["ir.attachment"].create({
                "name": self.media_filename or "whatsapp-media",
                "datas": self.media_data,
                "mimetype": self.media_mimetype or "application/octet-stream",
                "res_model": self._name,
                "res_id": self.id,
            })
        label = self.env._("WhatsApp (%s)", self.session_id.name)
        body = self.body or ""
        if not body and self.message_type != "text":
            body = self.env._("sent %s", self.message_type)
        self.partner_id.message_post(
            # Markup(...) % args escapes the args: inbound text is untrusted.
            body=Markup("<p><b>%s</b><br/>%s</p>") % (label, body),
            message_type="comment",
            attachment_ids=attachments.ids,
        )

    @api.model
    def cron_fetch_media(self):
        for rec in self.search([("media_state", "=", "pending")],
                               limit=20, order="create_date asc"):
            try:
                rec.action_fetch_media()
                rec.env.cr.commit()
            except Exception as exc:  # noqa: BLE001 - one bad file must not kill the cron
                rec.env.cr.rollback()
                _logger.warning("whatsmeow.message %s media cron failed: %s", rec.id, exc)
