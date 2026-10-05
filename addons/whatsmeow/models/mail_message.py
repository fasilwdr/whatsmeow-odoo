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
    # Stored on the entry rather than looked up from whatsmeow.message: the log
    # must keep saying where a message went after the contact's number changes
    # or the message row is purged.
    whatsmeow_recipient = fields.Char(
        string="WhatsApp Recipient", readonly=True, copy=False,
        help="Who an outgoing WhatsApp message was sent to, as shown beside "
             "the WhatsApp badge in the chatter.",
    )

    def _to_store_defaults(self, target):
        return super()._to_store_defaults(target) + ["whatsmeow_recipient"]


class MailThread(models.AbstractModel):
    _inherit = "mail.thread"

    def _get_message_create_valid_field_names(self):
        # message_post rejects any value it does not know; the recipient has to
        # be set at creation so it is already there when the client first
        # renders the entry.
        return super()._get_message_create_valid_field_names() | {"whatsmeow_recipient"}
