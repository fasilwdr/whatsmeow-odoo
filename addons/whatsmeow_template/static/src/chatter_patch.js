import { Chatter } from "@mail/chatter/web_portal/chatter";

import { _t } from "@web/core/l10n/translation";
import { patch } from "@web/core/utils/patch";
import { useService } from "@web/core/utils/hooks";
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
 */
patch(Chatter.prototype, {
    setup() {
        super.setup();
        this.action = useService("action");
    },

    get hasWhatsmeowButton() {
        // A record has to exist before it can be messaged: threadId is false
        // while a form is still being created.
        return Boolean(session.whatsmeow_can_send && this.props.threadId);
    },

    onClickWhatsmeow() {
        this.action.doAction(
            {
                type: "ir.actions.act_window",
                name: _t("Send WhatsApp"),
                res_model: "whatsmeow.composer",
                views: [[false, "form"]],
                view_mode: "form",
                target: "new",
                context: {
                    active_model: this.props.threadModel,
                    active_id: this.props.threadId,
                    active_ids: [this.props.threadId],
                },
            },
            {
                // The message lands on the record's chatter through the normal
                // inbound/outbound path, so refresh what the user is looking at.
                //
                // `requestList` alone is not enough: the form chatter's list is
                // activities/attachments/followers/… and *not* "messages", and
                // `Thread.fetchThreadData` only calls `fetchNewMessages()` when
                // "messages" is in it — so the log we just wrote never arrived
                // until the form itself was reloaded. Same shape as core's
                // `onActivityChanged`, plus the scroll core does after a post.
                onClose: () => {
                    this.state.jumpThreadPresent++;
                    this.load(this.state.thread, [...this.requestList, "messages"]);
                },
            }
        );
    },
});
