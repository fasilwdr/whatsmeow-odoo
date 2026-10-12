import {
    MENU_TABS,
    MessagingMenu,
} from "@mail/core/public_web/messaging_menu/messaging_menu_model";
import { fields } from "@mail/model/export";

import { _t } from "@web/core/l10n/translation";
import { patch } from "@web/core/utils/patch";
import { session } from "@web/session";

MENU_TABS.WHATSMEOW = "whatsmeow";

patch(MessagingMenu.prototype, {
    setup() {
        super.setup(...arguments);
        // Odoo 20 files every conversation under a tab — one list drives both
        // the systray menu and the Discuss sidebar — and a custom channel_type
        // belongs to none of the stock ones, so without a tab of its own a
        // WhatsApp conversation would be listed nowhere. A separate tab rather
        // than "Chats": these threads are not colleagues, they are customers
        // on someone else's phone, and an operator should be able to tell the
        // two apart at a glance.
        //
        // What the tab holds is decided twice, and the two must agree: here
        // for the channels already in the store, and server-side for the ones
        // still to load (`_get_menu_tab_domain` in controllers/).
        this.whatsmeowTab = fields.One("MessagingMenuTab", {
            compute() {
                if (this.store.self_user?.share !== false) {
                    return null;
                }
                // Only when there is something to list: an install whose
                // sessions all post to the chatter never routes a
                // conversation, and an empty tab is just one more thing to
                // explain. The session flag answers for the conversations not
                // loaded yet; the store catches the first one to arrive after
                // the page was opened.
                const hasConversations =
                    session.whatsmeow_has_conversations ||
                    Object.values(this.store["discuss.channel"].records).some(
                        (channel) => channel.channel_type === "whatsmeow"
                    );
                if (!hasConversations) {
                    return null;
                }
                return {
                    id: MENU_TABS.WHATSMEOW,
                    recordType: "discuss.channel",
                    includesChannel: (channel) =>
                        channel.channel_type === "whatsmeow" &&
                        Boolean(channel.self_member_id?.is_pinned || channel.isLocallyPinned),
                    icon: "oi_whatsapp",
                    sequence: 50,
                    label: _t("WhatsApp"),
                    emptyState: {
                        title: _t("No WhatsApp conversations yet."),
                    },
                    filters: [
                        {
                            id: "whatsmeow_unread",
                            text: _t("Unread"),
                            includesChannel: (channel) => channel.isUnread,
                        },
                    ],
                };
            },
            eager: true,
        });
    },
});
