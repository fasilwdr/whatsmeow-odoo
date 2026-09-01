from odoo import models


class MailMessage(models.Model):
    _inherit = "mail.message"

    # Odoo 16 has no single `_message_reaction` choke point (that arrived in
    # 17): the controller calls `_message_add_reaction` / `_message_remove_
    # reaction`, one per gesture, and both act for the *current* user. That is
    # exactly the gesture we want to relay, so each is hooked separately.

    def _message_add_reaction(self, content):
        res = super()._message_add_reaction(content)
        if not self.env.context.get("whatsmeow_skip_send"):
            self._whatsmeow_relay_reaction(content, "add")
        return res

    def _message_remove_reaction(self, content):
        res = super()._message_remove_reaction(content)
        if not self.env.context.get("whatsmeow_skip_send"):
            self._whatsmeow_relay_reaction(content, "remove")
        return res

    def _whatsmeow_relay_reaction(self, content, action):
        """Relay an operator's Discuss reaction out over WhatsApp.

        A reaction we applied from an inbound event carries
        `whatsmeow_skip_send` and never reaches here; the correspondent is not
        an internal user and cannot react in Discuss at all, so nothing loops.
        """
        self.ensure_one()
        if self.model != "mail.channel" or not self.res_id:
            return
        channel = self.env["mail.channel"].sudo().browse(self.res_id)
        if channel.channel_type != "whatsmeow":
            return
        # only an internal operator's reaction is an outgoing gesture
        partner = self.env.user.partner_id
        if not partner or self.env.user.share:
            return
        wa = self.env["whatsmeow.message"].sudo().search(
            [("mail_message_id", "=", self.id)], limit=1)
        if wa:
            # adding uses the emoji; removing clears it with an empty reaction
            wa._send_reaction(content if action == "add" else "")
