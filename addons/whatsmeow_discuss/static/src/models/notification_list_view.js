/** @odoo-module **/

import { registerPatch } from "@mail/model/model_core";

/**
 * What the messaging menu's WhatsApp tab lists.
 *
 * `filter` is the active tab id, so adding a tab is only ever adding a case
 * here — the pinned WhatsApp conversations, newest activity first, exactly the
 * ordering core applies to the chats.
 */
registerPatch({
    name: "NotificationListView",
    fields: {
        filteredChannels: {
            compute() {
                if (this.filter === "whatsmeow") {
                    return this.messaging.models["Channel"]
                        .all(
                            (channel) =>
                                channel.channel_type === "whatsmeow" &&
                                channel.thread.isPinned
                        )
                        .sort((c1, c2) => (c1.displayName < c2.displayName ? -1 : 1));
                }
                return this._super();
            },
        },
    },
});
