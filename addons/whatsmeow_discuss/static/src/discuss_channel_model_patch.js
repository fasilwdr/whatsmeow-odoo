import { DiscussChannel } from "@mail/discuss/core/common/discuss_channel_model";
import { patch } from "@web/core/utils/patch";

export const WHATSMEOW_AVATAR = "/whatsmeow_discuss/static/src/img/whatsapp.svg";

patch(DiscussChannel.prototype, {
    // Stock Discuss treats only "chat" and "group" as conversations between
    // people, and a good deal hangs on that: the messaging menu previews the
    // newest message of a chat channel but only the newest *needaction* one of
    // anything else — and a WhatsApp message is never a needaction, so its row
    // would show a name over an empty line. The unread badge on the row and on
    // the WhatsApp tab follows the same switch (`importantCounter`).
    get chatChannelTypes() {
        return [...super.chatChannelTypes, "whatsmeow"];
    },

    // The server lets an operator leave a conversation (reassignment, see
    // `_types_allowing_unfollow`); the client keeps a list of its own.
    get allowedToLeaveChannelTypes() {
        return [...super.allowedToLeaveChannelTypes, "whatsmeow"];
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
