import { Thread } from "@mail/core/common/thread_model";
import { patch } from "@web/core/utils/patch";

// A custom channel_type has no sidebar category out of the box
// (_computeDiscussAppCategory returns nothing for it), so a WhatsApp
// conversation would never appear in Discuss. File it under "Direct messages"
// so operators find their conversations alongside their chats.
patch(Thread.prototype, {
    _computeDiscussAppCategory() {
        if (this.channel_type === "whatsmeow") {
            return this.store.discuss.chats;
        }
        return super._computeDiscussAppCategory();
    },
});
