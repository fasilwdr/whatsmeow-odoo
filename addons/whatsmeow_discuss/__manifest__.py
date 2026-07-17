{
    "name": "Whatsmeow Discuss Routing",
    "version": "19.0.1.1.0",
    "summary": "Attend WhatsApp conversations in Odoo Discuss, with rule-based routing",
    "description": """
Turn accepted inbound WhatsApp messages into live Discuss conversations.

Each WhatsApp conversation (session + chat) maps to one discuss.channel: inbound
messages post into it, operators reply by typing in the thread, and per-session
routing rules decide which operators attend a new conversation. Opt-in per
session; a session with routing off keeps posting to the partner's chatter.
""",
    "author": "Fasil",
    "category": "Discuss",
    "depends": ["whatsmeow", "mail"],
    "data": [
        "security/ir.model.access.csv",
        "views/whatsmeow_session_views.xml",
    ],
    "assets": {
        "web.assets_backend": [
            "whatsmeow_discuss/static/src/discuss_sidebar_category_patch.js",
        ],
    },
    "license": "LGPL-3",
    "installable": True,
    "application": False,
}
