/** @odoo-module **/

import { registerPatch } from "@mail/model/model_core";

/**
 * A custom channel_type has no sidebar category out of the box
 * (`Channel.discussSidebarCategory` returns nothing for it), so a WhatsApp
 * conversation would never appear in Discuss. File it under "Direct messages"
 * so operators find their conversations alongside their chats — the same hook
 * `im_livechat` uses for its own type.
 */
registerPatch({
    name: "Channel",
    fields: {
        discussSidebarCategory: {
            compute() {
                if (this.channel_type === "whatsmeow") {
                    return this.messaging.discuss.categoryChat;
                }
                return this._super();
            },
        },
    },
});
