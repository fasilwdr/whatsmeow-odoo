import logging
import re

from odoo import api, fields, models
from odoo.exceptions import ValidationError
from odoo.tools import SQL

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
    body = fields.Text(required=True)
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

    def action_send(self):
        for rec in self.filtered(
            lambda r: r.direction == "out" and r.state in ("outgoing", "error")
        ):
            try:
                data = rec.session_id._gw(
                    "POST", f"/sessions/{rec.session_id.code}/send",
                    {"phone": rec.phone, "message": rec.body},
                )
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
