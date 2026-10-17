import { _t } from "@web/core/l10n/translation";
import { Component, onMounted, onWillUnmount, proxy, useProps, t } from "@odoo/owl";
import { Dialog } from "@web/core/dialog/dialog";
import { usePos } from "@point_of_sale/app/hooks/pos_hook";

// WhatsApp rotates the pairing code about every 20 seconds; asking every few
// seconds keeps the one on screen scannable without hammering the gateway.
const POLL_MS = 3000;

export function rpcErrorMessage(error) {
    return error?.data?.message || error?.message || _t("Something went wrong.");
}

/**
 * Pair a WhatsApp number from the register: start a session on the gateway,
 * show its QR code until the phone scans it, then hand back to the caller.
 */
export class WhatsmeowPairDialog extends Component {
    static template = "whatsmeow_pos.WhatsmeowPairDialog";
    static components = { Dialog };
    props = useProps({
        onConnected: t.function().optional(),
        close: t.function(),
    });

    setup() {
        this.pos = usePos();
        this.state = proxy({ status: "starting", qr: false, session: "", error: "" });
        this.sessionId = null;
        this.timer = null;
        this.stopped = false;
        onMounted(() => this.start());
        onWillUnmount(() => {
            this.stopped = true;
            clearTimeout(this.timer);
        });
    }

    get qrSrc() {
        return this.state.qr ? `data:image/png;base64,${this.state.qr}` : "";
    }

    async start() {
        await this.ask("whatsmeow_pos_pair_start", []);
    }

    async poll() {
        await this.ask("whatsmeow_pos_pair_poll", [this.sessionId]);
    }

    async ask(method, args) {
        let result;
        try {
            result = await this.pos.data.call("pos.config", method, [
                [this.pos.config.id],
                ...args,
            ]);
        } catch (error) {
            if (!this.stopped) {
                this.state.status = "failed";
                this.state.error = rpcErrorMessage(error);
            }
            return;
        }
        if (this.stopped) {
            return;
        }
        this.sessionId = result.session_id;
        Object.assign(this.state, {
            status: result.status,
            qr: result.qr,
            session: result.session,
            error: result.error || "",
        });
        if (result.status === "connected") {
            this.props.close();
            this.props.onConnected?.();
            return;
        }
        this.timer = setTimeout(() => this.poll(), POLL_MS);
    }

    retry() {
        clearTimeout(this.timer);
        Object.assign(this.state, { status: "starting", qr: false, error: "" });
        this.start();
    }
}
