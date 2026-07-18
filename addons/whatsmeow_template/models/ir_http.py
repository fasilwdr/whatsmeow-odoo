from odoo import models


class IrHttp(models.AbstractModel):
    _inherit = "ir.http"

    def session_info(self):
        """Tell the web client whether the chatter's WhatsApp button is usable.

        Answered once at load instead of per record: the button appears on every
        mail.thread form, so an RPC per chatter would be a lot of traffic to
        learn something that only changes when a session is configured.
        """
        info = super().session_info()
        info["whatsmeow_can_send"] = bool(
            self.env.user.has_group("whatsmeow.group_whatsmeow_user")
            and self.env["whatsmeow.session"].sudo().search_count([], limit=1)
        )
        return info
