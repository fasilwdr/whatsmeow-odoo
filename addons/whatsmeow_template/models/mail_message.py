from odoo import fields, models


class MailMessage(models.Model):
    _inherit = "mail.message"

    message_type = fields.Selection(
        selection_add=[("whatsmeow", "WhatsApp Message")],
        ondelete={"whatsmeow": "set default"},
    )
