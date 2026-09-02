import { fields } from "@mail/core/common/record";
import { DiscussApp } from "@mail/core/public_web/discuss_app_model";

import { _t } from "@web/core/l10n/translation";
import { patch } from "@web/core/utils/patch";

patch(DiscussApp.prototype, {
    setup(env) {
        super.setup(...arguments);
        // The sidebar section WhatsApp conversations are filed under. Hidden
        // while empty, so an install whose sessions all post to the chatter
        // sees no trace of it.
        this.whatsmeowCategory = fields.One("DiscussAppCategory", {
            compute() {
                return {
                    extraClass: "o-mail-DiscussSidebarCategory-whatsmeow",
                    hideWhenEmpty: true,
                    icon: "fa fa-whatsapp",
                    id: "whatsmeow_discuss.category_conversations",
                    name: _t("WhatsApp"),
                    sequence: 22,
                };
            },
            eager: true,
        });
    },
});
