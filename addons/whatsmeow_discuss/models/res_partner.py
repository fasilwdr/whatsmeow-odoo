from odoo import models


class ResPartner(models.Model):
    _inherit = "res.partner"

    def _get_channels_as_member(self):
        """Include WhatsApp conversations in what Discuss loads for a partner.

        This is the gate that decides which channels reach the browser at all:
        Odoo 16 hard-codes `channel`/`group`/`chat` here, so a conversation of
        our own `channel_type` is created server-side and then never sent to
        the client — the operator sees nothing in Discuss however the routing
        rules matched. `im_livechat` extends this exact method for the same
        reason, and this is the same query with our type.

        Pinned only, like the chats: unpinning a conversation is how an
        operator puts it away, and a new inbound message pins it again (see
        `mail.channel.message_post`).
        """
        channels = super()._get_channels_as_member()
        channels |= self.env["mail.channel"].search([
            ("channel_type", "=", "whatsmeow"),
            ("channel_member_ids", "in", self.env["mail.channel.member"].sudo()._search([
                ("partner_id", "=", self.id),
                ("is_pinned", "=", True),
            ])),
        ])
        return channels
