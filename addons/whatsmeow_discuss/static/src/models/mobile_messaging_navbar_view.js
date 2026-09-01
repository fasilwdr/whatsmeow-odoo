/** @odoo-module **/

import { registerPatch } from "@mail/model/model_core";

/**
 * The same WhatsApp tab on a small screen, where the messaging menu draws its
 * tabs from this list instead of the desktop template. Only the menu's navbar
 * gets it: Discuss's own mobile navbar is Mailboxes/Chat/Channel, and a
 * WhatsApp conversation is already reachable there under Chat.
 */
registerPatch({
    name: "MobileMessagingNavbarView",
    fields: {
        tabs: {
            compute() {
                const tabs = this._super();
                if (this.messagingMenu) {
                    return [
                        ...tabs,
                        {
                            icon: "fa fa-whatsapp",
                            id: "whatsmeow",
                            label: this.env._t("WhatsApp"),
                        },
                    ];
                }
                return tabs;
            },
        },
    },
});
