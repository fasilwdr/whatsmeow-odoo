import { Message } from "@mail/core/common/message_model";
import { patch } from "@web/core/utils/patch";

patch(Message.prototype, {
    setup() {
        super.setup(...arguments);
        /** @type {string|undefined} who an outgoing WhatsApp send went to (see message_patch.xml) */
        this.whatsmeow_recipient = undefined;
    },
});
