from odoo.tests import HttpCase, tagged
from odoo.tools import mute_logger

from odoo.addons.whatsmeow.controllers.webhook import WhatsmeowWebhook

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
class TestDiscussUI(HttpCase):
    """The web-client half of the bridge, in a real browser.

    Everything here is a client-side patch of a core Discuss model or template,
    which a Python test cannot see break: a renamed getter or a moved template
    still installs and still passes every server test, and the conversation
    simply stops being listed.
    """

    def _inbound_conversation(self):
        """One routed conversation attended by the admin, as the gateway's
        webhook would leave it. (Not `self.session`: that name is the HTTP
        session of an HttpCase.)"""
        connection = self.env["whatsmeow.connection"].create({
            "name": "GW", "base_url": "http://127.0.0.1:8080",
            "api_key": "k", "webhook_secret": "s",
        })
        wa_session = self.env["whatsmeow.session"].create({
            "name": "ACME", "code": "acme-ui", "connection_id": connection.id,
            "route_to_discuss": True,
            "route_fallback_user_ids": [(6, 0, self.env.ref("base.user_admin").ids)],
        })
        self.env["res.partner"].create({
            "name": "Fasil Wandoor", "phone": "+44 7700 900001"})
        with mute_logger("odoo.addons.whatsmeow.controllers.webhook"):
            WhatsmeowWebhook()._on_message(self.env, wa_session, {
                "wa_message_id": "WA-UI-1",
                "sender_phone": "447700900001",
                "sender_jid": "447700900001@s.whatsapp.net",
                "chat_jid": "447700900001@s.whatsapp.net",
                "push_name": "fasil",
                "body": "hi from whatsapp",
            })
        return self.env["discuss.channel"].search([
            ("channel_type", "=", "whatsmeow"),
            ("whatsmeow_session_id", "=", wa_session.id),
        ])

    def test_a_conversation_is_listed_under_the_whatsapp_tab(self):
        self.assertTrue(self._inbound_conversation())

        self.browser_js(
            "/odoo/action-mail.action_discuss",
            UNTIL + """
(async () => {
    const tab = await until(
        () => document.querySelector(".o-mail-MessagingMenu-tab[data-id='whatsmeow']"),
        "the WhatsApp tab");
    if (tab.querySelector("i").dataset.icon !== "oi_whatsapp") {
        throw new Error("the tab does not carry the WhatsApp icon");
    }
    tab.click();
    const row = await until(
        () => [...document.querySelectorAll(".o-mail-MessagingMenuItem")].find(
            (el) => el.textContent.includes("Fasil Wandoor (+447700900001)")),
        "the conversation in the WhatsApp tab");
    if (!row.querySelector("img[src*='whatsapp.svg']")) {
        throw new Error("the conversation does not draw the WhatsApp avatar");
    }
    if (!row.textContent.includes("hi from whatsapp")) {
        throw new Error("the row does not preview the newest message");
    }
    row.querySelector(".o-mail-NotificationItem").click();
    const bubble = await until(
        () => [...document.querySelectorAll(".o-mail-Message")].find(
            (el) => el.textContent.includes("hi from whatsapp")),
        "the inbound bubble");
    const author = bubble.querySelector(".o-mail-Message-author");
    if (!author || !author.textContent.includes("Fasil Wandoor (+447700900001)")) {
        throw new Error("the bubble does not name the sender with their number");
    }
    // Replying is the point of routing a conversation here.
    await until(
        () => document.querySelector(".o-mail-Composer-input"),
        "the reply box");
    console.log("test successful");
})().catch((error) => console.error(error.message));
""",
            login="admin",
        )

    def test_no_tab_without_a_conversation(self):
        self.assertFalse(self.env["discuss.channel"].search_count(
            [("channel_type", "=", "whatsmeow")]))
        self.browser_js(
            "/odoo/action-mail.action_discuss",
            UNTIL + """
(async () => {
    await until(
        () => document.querySelector(".o-mail-MessagingMenu-tab"),
        "the stock tabs");
    if (document.querySelector(".o-mail-MessagingMenu-tab[data-id='whatsmeow']")) {
        throw new Error("an empty WhatsApp tab is shown");
    }
    console.log("test successful");
})().catch((error) => console.error(error.message));
""",
            login="admin",
        )
