from odoo import fields, models


class MailMessage(models.Model):
    """Mark the chatter entries that are WhatsApp traffic.

    A dedicated `message_type` rather than a flag on the body: it is what the
    web client already reads to render a message, so the badge and the tinted
    bubble (see `static/src/message_patch.xml`) cost one attribute check and no
    extra RPC. Core sets it on the inbound posts it makes to a contact's
    chatter; `whatsmeow_template` sets the same type when it logs an outgoing
    templated send on its source record, so both directions read alike in a
    thread full of ordinary notes.
    """
    _inherit = "mail.message"

    message_type = fields.Selection(
        selection_add=[("whatsmeow", "WhatsApp Message")],
        ondelete={"whatsmeow": "set default"},
    )
