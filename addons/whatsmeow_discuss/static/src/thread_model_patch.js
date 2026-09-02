import { Thread } from "@mail/core/common/thread_model";
import { patch } from "@web/core/utils/patch";

export const WHATSMEOW_AVATAR = "/whatsmeow_discuss/static/src/img/whatsapp.svg";

patch(Thread.prototype, {
    // A custom channel_type has no sidebar category out of the box
    // (_computeDiscussAppCategory returns nothing for it), so a WhatsApp
    // conversation would never appear in Discuss. Give it its own section
    // rather than filing it under "Direct messages": these threads are not
    // colleagues, they are customers on someone else's phone, and an operator
    // scanning the sidebar should be able to tell the two apart at a glance.
    _computeDiscussAppCategory() {
        if (this.channel_type === "whatsmeow") {
            return this.store.discuss.whatsmeowCategory;
        }
        return super._computeDiscussAppCategory();
    },

    // Without this a WhatsApp conversation draws Odoo's default avatar, exactly
    // like an internal chat: the systray, the sidebar and the chat window all
    // read `avatarUrl`. A channel-type mark is the one that stays right — the
    // correspondent is often a contact we have never met and have no photo of,
    // and in a group there is no single face to show anyway.
    get avatarUrl() {
        if (this.channel_type === "whatsmeow") {
            return WHATSMEOW_AVATAR;
        }
        return super.avatarUrl;
    },
});
