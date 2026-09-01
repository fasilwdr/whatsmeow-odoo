/** @odoo-module **/

import { registerPatch } from "@mail/model/model_core";

/**
 * The chat category also decides which channel is highlighted as the active
 * one (`activeItem` checks `supportedChannelTypes`). Without this, opening a
 * WhatsApp conversation would show it in the sidebar but never mark it
 * selected.
 */
registerPatch({
    name: "DiscussSidebarCategory",
    fields: {
        supportedChannelTypes: {
            compute() {
                if (this.discussAsChat) {
                    return [...this._super(), "whatsmeow"];
                }
                return this._super();
            },
        },
    },
});
