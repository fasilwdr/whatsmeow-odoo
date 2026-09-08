{
    "name": "Whatsmeow Chatbot",
    "version": "16.0.1.1.0",
    "summary": "Build WhatsApp chatbot workflows from the UI and collect replies",
    "description": """
A rule-triggered chatbot for the whatsmeow WhatsApp connector.

A bot answers on behalf of a dedicated user (or OdooBot), decides whether it
should speak using the very same match criteria as inbound filtering and Discuss
routing, walks the contact through a workflow of steps built in a drag-and-drop
designer, stores every answer on the chat, fires server actions from what it
collected, and hands the conversation over to real operators when the script
says so — reusing the session's existing Discuss routing rules to pick them.

Every chat the bot ever held is a record: transcript, collected values, current
step and outcome, visible to WhatsApp administrators from their own menu.
""",
    "author": "Fasil, Bytesraw",
    "category": "Discuss",
    "depends": ["whatsmeow", "whatsmeow_discuss", "mail", "web"],
    "data": [
        "security/ir.model.access.csv",
        "data/whatsmeow_bot_data.xml",
        "data/ir_cron.xml",
        "views/whatsmeow_bot_views.xml",
        "views/whatsmeow_bot_chat_views.xml",
        "views/whatsmeow_session_views.xml",
        "views/menus.xml",
    ],
    "assets": {
        "web.assets_backend": [
            "whatsmeow_bot/static/src/builder/flow_builder.scss",
            "whatsmeow_bot/static/src/builder/markup.js",
            "whatsmeow_bot/static/src/builder/flow_builder.js",
            "whatsmeow_bot/static/src/builder/flow_builder.xml",
        ],
    },
    "license": "LGPL-3",
    "installable": True,
    "application": False,
}
