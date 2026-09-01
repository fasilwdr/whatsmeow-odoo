/** @odoo-module **/

import { registerPatch } from "@mail/model/model_core";
import { attr } from "@mail/model/model_field";
import { session } from "@web/session";

/**
 * Put a WhatsApp button in the chatter of every mail.thread model, next to
 * "Send message" and "Log note".
 *
 * Enterprise's whatsapp module shows the button wherever a record can be
 * messaged; we do the same rather than binding per model, because a chatter is
 * exactly the set of records that have a correspondent worth writing to. The
 * button only renders when the session says WhatsApp is usable at all — see
 * `ir.http.session_info` — so an install with no gateway configured looks
 * untouched.
 *
 * Odoo 16's chatter is a messaging *model* (`Chatter`) rendered by the
 * `ChatterTopbar` component, so the button lives here rather than on a
 * component class.
 */
registerPatch({
    name: "Chatter",
    recordMethods: {
        onClickWhatsmeow() {
            this.env.services.action.doAction(
                {
                    type: "ir.actions.act_window",
                    name: this.env._t("Send WhatsApp"),
                    res_model: "whatsmeow.composer",
                    views: [[false, "form"]],
                    view_mode: "form",
                    target: "new",
                    context: {
                        active_model: this.threadModel,
                        active_id: this.threadId,
                        active_ids: [this.threadId],
                    },
                },
                {
                    // The send is logged on the record's chatter through the
                    // normal outbound path, so refresh what the user is looking
                    // at once the composer closes.
                    onClose: () => {
                        if (this.exists() && this.thread) {
                            this.refresh();
                        }
                    },
                }
            );
        },
    },
    fields: {
        hasWhatsmeowButton: attr({
            compute() {
                // A record has to exist before it can be messaged: threadId is
                // false while a form is still being created.
                return Boolean(session.whatsmeow_can_send && this.threadId);
            },
        }),
    },
});
