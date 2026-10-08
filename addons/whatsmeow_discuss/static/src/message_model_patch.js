import { Message } from "@mail/core/common/message_model";
import { patch } from "@web/core/utils/patch";

patch(Message.prototype, {
    // A WhatsApp conversation is titled "Name (+number)", but a bubble inside
    // it would show the contact's name alone — less than the sidebar says, in
    // the one place an operator is actually reading. The server stores the
    // full label on each inbound bubble (`whatsmeow_author_label`); preferring
    // it here, rather than in the message template, keeps every surface that
    // names an author in step: the bubble, the "Replying to" bar, a quoted
    // reply, the messaging-menu preview and the desktop notification.
    //
    // Only inbound WhatsApp bubbles carry the label, so an operator's own
    // reply — and every message outside a WhatsApp conversation — falls
    // through to the stock name.
    get authorName() {
        return this.whatsmeow_author_label || super.authorName;
    },
});
