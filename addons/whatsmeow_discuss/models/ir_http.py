from odoo import models


class IrHttp(models.AbstractModel):
    _inherit = "ir.http"

    def session_info(self):
        """Tell the web client whether to show the WhatsApp tab at all.

        Conversations are loaded lazily, a tab at a time, so the client cannot
        tell an empty tab from one it has not opened yet. Answered once at load:
        an install whose sessions all post to the chatter shows no tab, and the
        first conversation to arrive afterwards reveals it client-side.
        """
        info = super().session_info()
        if self.env.user._is_internal():
            info["whatsmeow_has_conversations"] = bool(
                self.env["discuss.channel.member"].search_count([
                    ("is_self", "=", True),
                    ("channel_id.channel_type", "=", "whatsmeow"),
                ], limit=1)
            )
        return info
