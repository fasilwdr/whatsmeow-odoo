from odoo import models


class MailMessage(models.Model):
    _inherit = "mail.message"

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
