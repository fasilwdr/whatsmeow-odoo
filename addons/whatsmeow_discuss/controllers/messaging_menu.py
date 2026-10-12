from odoo.fields import Domain

from odoo.addons.mail.controllers.discuss.messaging_menu import DiscussMessagingMenuController


class WhatsmeowMessagingMenuController(DiscussMessagingMenuController):
    """Server half of the WhatsApp tab in the messaging menu / Discuss sidebar.

    A tab's content and counter are loaded lazily by its id, and an id the
    server does not know is a BadRequest — so the tab the web client declares
    (`static/src/messaging_menu_model_patch.js`) needs its domain here. The two
    must describe the same set of channels.
    """

    def _get_menu_tab_domain(self, tab_id):
        if tab_id == "whatsmeow":
            return Domain([
                ("channel_type", "=", "whatsmeow"),
                ("self_member_id.is_pinned", "=", True),
            ])
        return super()._get_menu_tab_domain(tab_id)

    def _get_menu_tab_filter_domain(self, tab_id, filter_id):
        if tab_id == "whatsmeow" and filter_id == "whatsmeow_unread":
            return Domain("self_member_id.is_unread", "=", True)
        return super()._get_menu_tab_filter_domain(tab_id, filter_id)
