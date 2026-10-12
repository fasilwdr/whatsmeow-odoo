from odoo.tests import HttpCase, tagged


# Wait for something to show up, the way a tour step does: the web client
# renders asynchronously, so every assertion is a poll with a deadline.
UNTIL = """
const until = async (find, what) => {
    for (let i = 0; i < 300; i++) {
        const found = find();
        if (found) {
            return found;
        }
        await new Promise((resolve) => setTimeout(resolve, 50));
    }
    throw new Error("timed out waiting for " + what);
};
"""


@tagged("post_install", "-at_install")
class TestChatterUI(HttpCase):
    """The chatter button, the composer's body widget and the WhatsApp badge,
    in a real browser.

    All three hang off core templates and components (`mail.Chatter`,
    `mail.Message`, `TextField`) that a Python test cannot see move.
    """

    def test_send_from_the_chatter(self):
        # The button only shows once a number is paired. (Not `self.session`:
        # that name is the HTTP session of an HttpCase.)
        connection = self.env["whatsmeow.connection"].create({
            "name": "GW", "base_url": "http://127.0.0.1:8081",
            "api_key": "k", "webhook_secret": "s",
        })
        self.env["whatsmeow.session"].create({
            "name": "ACME", "code": "acme-ui", "connection_id": connection.id,
        })
        alice = self.env["res.partner"].create({
            "name": "Alice", "phone": "+44 7700 900123",
        })
        # An entry already in the log, as a templated send leaves it.
        alice.message_post(
            body="an earlier send", message_type="whatsmeow",
            subtype_xmlid="mail.mt_note", whatsmeow_recipient="Sales Team")

        self.browser_js(
            f"/odoo/action-base.action_partner_form/{alice.id}",
            UNTIL + """
(async () => {
    // -- the log: badge, tint hook and recipient ------------------------------
    const entry = await until(
        () => document.querySelector(".o-mail-Message[data-whatsmeow='1']"),
        "the WhatsApp entry in the chatter");
    const badge = entry.querySelector(".o-whatsmeow-badge");
    if (!badge || badge.dataset.icon !== "oi_whatsapp") {
        throw new Error("the entry is not badged as WhatsApp");
    }
    const recipient = entry.querySelector(".o-whatsmeow-recipient-name");
    if (!recipient || recipient.textContent !== "Sales Team") {
        throw new Error("the entry does not name its recipient");
    }

    // -- the button opens the composer ----------------------------------------
    const button = await until(
        () => document.querySelector(".o-mail-Chatter-whatsmeow"),
        "the WhatsApp button");
    button.click();
    const textarea = await until(
        () => document.querySelector(".modal .o_whatsmeow_body textarea"),
        "the composer's body field");

    // -- the body widget: toolbar on selection, preview underneath ------------
    textarea.focus();
    textarea.value = "hello world";
    textarea.dispatchEvent(new InputEvent("input", { bubbles: true }));
    textarea.setSelectionRange(0, 5);
    textarea.dispatchEvent(new Event("select", { bubbles: true }));
    const bold = await until(
        () => document.querySelector(".modal .o_whatsmeow_body_toolbar button"),
        "the formatting toolbar");
    if (bold.querySelector("i").dataset.icon !== "format_bold") {
        throw new Error("the first toolbar button is not bold");
    }
    bold.click();
    await until(() => textarea.value === "*hello* world", "the bold markers");
    await until(
        () => document.querySelector(".modal .o_whatsmeow_body_preview_bubble strong, " +
                                     ".modal .o_whatsmeow_body_preview_bubble b"),
        "the preview to draw the bold");

    // Nothing is sent: leave no half-written form behind.
    document.querySelector(".modal button[special='cancel']").click();
    await until(() => !document.querySelector(".modal"), "the composer to close");
    console.log("test successful");
})().catch((error) => console.error(error.message));
""",
            login="admin",
        )
