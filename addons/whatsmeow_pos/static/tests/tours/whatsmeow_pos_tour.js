import * as Chrome from "@point_of_sale/../tests/pos/tours/utils/chrome_util";
import * as Dialog from "@point_of_sale/../tests/generic_helpers/dialog_util";
import * as FeedbackScreen from "@point_of_sale/../tests/pos/tours/utils/feedback_screen_util";
import * as PaymentScreen from "@point_of_sale/../tests/pos/tours/utils/payment_screen_util";
import * as ProductScreen from "@point_of_sale/../tests/pos/tours/utils/product_screen_util";
import { registry } from "@web/core/registry";

function payAndOpenSend() {
    return [
        Chrome.startPoS(),
        Dialog.confirm("Open Register"),
        ProductScreen.addOrderline("Letter Tray", "10", "5"),
        ProductScreen.clickPartnerButton(),
        ProductScreen.clickCustomer("APartner Full"),
        ProductScreen.clickPayButton(),
        PaymentScreen.clickPaymentMethod("Bank"),
        PaymentScreen.clickValidate(),
        FeedbackScreen.isShown(),
        FeedbackScreen.clickSendButton(),
    ].flat();
}

const whatsappButton = ".modal-body button:has(.send-receipt-whatsmeow)";

function typeNumberAndSend() {
    return [
        {
            content: "the cashier can change the number",
            trigger: ".modal-body .send-receipt-phone-input",
            run: "edit +44 7700 900123",
        },
        {
            content: "send over WhatsApp",
            trigger: `${whatsappButton}:enabled`,
            run: "click",
        },
    ];
}

registry.category("web_tour.tours").add("WhatsmeowPosSendTour", {
    steps: () =>
        [
            payAndOpenSend(),
            typeNumberAndSend(),
            { trigger: ".modal-body .notice .text-success" },
        ].flat(),
});

registry.category("web_tour.tours").add("WhatsmeowPosPairTour", {
    steps: () =>
        [
            payAndOpenSend(),
            {
                content: "no number yet, and a manager is offered to pair one",
                trigger: ".modal-body .whatsmeow-pair-link",
            },
            typeNumberAndSend(),
            {
                content: "the pairing QR code is shown",
                trigger: ".whatsmeow-pair-dialog img.whatsmeow-pair-qr",
            },
            {
                content: "once scanned, the receipt goes out by itself",
                trigger: ".modal-body .notice .text-success",
            },
            {
                trigger: "body:not(:has(.whatsmeow-pair-dialog))",
            },
        ].flat(),
});
