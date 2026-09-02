import { MessagingMenu } from "@mail/core/public_web/messaging_menu";

import { _t } from "@web/core/l10n/translation";
import { patch } from "@web/core/utils/patch";

patch(MessagingMenu.prototype, {
    /**
     * WhatsApp conversations are not "Chats" (`tabToThreadType('chat')` is
     * chat + group), so today they can only be found under Notifications,
     * mixed in with every other unread thread. Give them their own filter.
     *
     * The tab id *is* the channel_type, which is what makes the default
     * `tabToThreadType` — it returns `[tab]` — filter the list correctly with
     * no further patch.
     */
    get hasWhatsmeowThreads() {
        return Object.values(this.store.Thread.records).some(
            ({ channel_type }) => channel_type === "whatsmeow"
        );
    },
    get whatsmeowUnreadCount() {
        return Object.values(this.store.Thread.records).reduce(
            (acc, thread) =>
                thread.channel_type === "whatsmeow" &&
                thread.self_member_id?.message_unread_counter > 0
                    ? acc + 1
                    : acc,
            0
        );
    },
    /** @override — the mobile navbar builds its buttons from this list. */
    get _tabs() {
        const items = super._tabs;
        // Only when there is something to filter: an install whose sessions
        // post to the chatter never routes a conversation, and an empty tab
        // is just one more thing to explain.
        if (this.hasWhatsmeowThreads) {
            items.push({
                counter: this.whatsmeowUnreadCount,
                icon: "fa fa-whatsapp",
                id: "whatsmeow",
                label: _t("WhatsApp"),
                sequence: 50,
            });
        }
        return items;
    },
});
