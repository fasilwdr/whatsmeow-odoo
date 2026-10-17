import { patch } from "@web/core/utils/patch";
import { proxy } from "@odoo/owl";
import { SendReceiptPopup } from "@point_of_sale/app/components/popups/send_receipt_popup/send_receipt_popup";
import { WhatsmeowPairDialog, rpcErrorMessage } from "./pair_dialog/pair_dialog";

const SEND_ACTION = "action_send_receipt_whatsmeow";

patch(SendReceiptPopup.prototype, {
    setup() {
        super.setup(...arguments);
        // Whether a number is connected is the gateway's to say, and it changes
        // under a running register, so it is asked when the dialog opens rather
        // than read from the data loaded with the session.
        this.whatsmeow = proxy({ checked: false, ready: false, canPair: false, session: "" });
        if (this.pos.config.whatsmeow_enabled) {
            this.whatsmeowLoadState();
        }
    },

    async whatsmeowLoadState() {
        try {
            const state = await this.pos.data.call("pos.config", "whatsmeow_pos_state", [
                [this.pos.config.id],
            ]);
            Object.assign(this.whatsmeow, {
                ready: state.ready,
                canPair: state.can_pair,
                session: state.session,
            });
        } catch {
            // Not knowing must not cost the email button: leave WhatsApp
            // offered, and let the send itself report what is wrong.
            this.whatsmeow.ready = true;
        } finally {
            this.whatsmeow.checked = true;
        }
    },

    showPhoneInput() {
        return super.showPhoneInput() || this.pos.config.whatsmeow_enabled;
    },

    get whatsmeowNeedsPairing() {
        return this.whatsmeow.checked && !this.whatsmeow.ready && this.whatsmeow.canPair;
    },

    actionSendReceiptOnWhatsmeow() {
        if (this.whatsmeowNeedsPairing) {
            // Nothing to send from yet: pair a number, then send what the
            // cashier asked for, without making them press the button again.
            this.whatsmeowPair(() => this.whatsmeowSend());
            return;
        }
        this.whatsmeowSend();
    },

    whatsmeowSend() {
        this.sendReceipt.call({
            action: SEND_ACTION,
            destination: this.state.phone,
            name: "WhatsApp",
        });
    },

    whatsmeowPair(then) {
        this.dialog.add(WhatsmeowPairDialog, {
            onConnected: () => {
                this.whatsmeow.ready = true;
                this.whatsmeowLoadState();
                then?.();
            },
        });
    },

    get whatsmeowError() {
        const { status, lastArgs, result } = this.sendReceipt;
        if (status !== "error" || lastArgs?.[0]?.action !== SEND_ACTION || !result) {
            return "";
        }
        return rpcErrorMessage(result);
    },

    async _sendReceiptToCustomer({ action, destination }) {
        if (action !== SEND_ACTION || !this.order.isSynced) {
            return super._sendReceiptToCustomer(...arguments);
        }
        // The server answers a refused send instead of raising it, so the
        // messages survive to be resent; turn that answer back into a failure
        // for the dialog.
        const result = await this.pos.data.call("pos.order", action, [
            [this.order.id],
            destination,
        ]);
        if (result?.error) {
            throw new Error(result.error);
        }
    },

    get sendList() {
        const list = super.sendList;
        if (this.pos.config.whatsmeow_enabled) {
            list.find((item) => item.model === "phone").buttons.push({
                click: () => this.actionSendReceiptOnWhatsmeow(),
                status:
                    this.sendReceipt.lastArgs?.[0]?.action === SEND_ACTION &&
                    this.sendReceipt.status,
                icon: "oi_whatsapp",
                iconClass: "oi-lg send-receipt-whatsmeow",
                disabled: () =>
                    !this.isValidPhone ||
                    (this.whatsmeow.checked && !this.whatsmeow.ready && !this.whatsmeow.canPair),
            });
        }
        return list;
    },
});
