from odoo import fields, models


class MailMessage(models.Model):
    _inherit = "mail.message"

    # Stored on the bubble rather than derived from the author when it is
    # drawn: the number a message came from is a fact about that message, and
    # must keep reading the same after the contact is renamed, merged or given
    # a different phone — the same reasoning as core's `whatsmeow_recipient`.
    whatsmeow_author_label = fields.Char(
        string="WhatsApp Sender", readonly=True, copy=False,
        help="How the sender of an inbound WhatsApp message is named above its "
             "Discuss bubble: the name together with the number, the way the "
             "conversation itself is titled.",
    )

    def _to_store_defaults(self, target):
        return super()._to_store_defaults(target) + ["whatsmeow_author_label"]

    def _message_reaction(self, content, action, partner, guest, store=None):
        """Relay an operator's Discuss reaction out over WhatsApp.

        `_message_reaction` is the single choke point every reaction passes
        through (the controller calls it as sudo). We let it persist normally,
        then send WhatsApp — but only for a real operator gesture: a reaction we
        applied from an inbound event carries `whatsmeow_skip_send`, and the
        correspondent's reaction is not an internal user, so neither loops out.
        """
        res = super()._message_reaction(content, action, partner, guest, store=store)
        if not self.env.context.get("whatsmeow_skip_send"):
            self._whatsmeow_relay_reaction(content, action, partner)
        return res

    def _whatsmeow_relay_reaction(self, content, action, partner):
        self.ensure_one()
        if self.model != "discuss.channel" or not self.res_id:
            return
        channel = self.env["discuss.channel"].browse(self.res_id)
        if channel.channel_type != "whatsmeow":
            return
        # only an internal operator's reaction is an outgoing gesture
        if not partner or not any(not user.share for user in partner.user_ids):
            return
        wa = self.env["whatsmeow.message"].sudo().search(
            [("mail_message_id", "=", self.id)], limit=1)
        if wa:
            # adding uses the emoji; removing clears it with an empty reaction
            wa._send_reaction(content if action == "add" else "")


class MailThread(models.AbstractModel):
    _inherit = "mail.thread"

    def _get_message_create_valid_field_names(self):
        # message_post rejects any value it does not know; the label has to be
        # set at creation so it is already there when the client first draws
        # the bubble.
        return super()._get_message_create_valid_field_names() | {
            "whatsmeow_author_label"}
