{
    "name": "Whatsmeow Discuss Routing",
    "version": "16.0.1.1.0",
    "summary": "Attend WhatsApp conversations in Odoo Discuss, with rule-based routing",
    "description": """
Turn accepted inbound WhatsApp messages into live Discuss conversations.

Each WhatsApp conversation (session + chat) maps to one mail.channel: inbound
messages post into it, operators reply by typing in the thread, and per-session
routing rules decide which operators attend a new conversation. Opt-in per
session; a session with routing off keeps posting to the partner's chatter.
""",
    "author": "Fasil, Bytesraw",
    "category": "Discuss",
    "depends": ["whatsmeow", "mail"],
    "data": [
        "security/ir.model.access.csv",
        "views/whatsmeow_session_views.xml",
    ],
    'images': [
        'static/description/banner.gif'
    ],
    "assets": {
        "web.assets_backend": [
            "whatsmeow_discuss/static/src/models/*.js",
            "whatsmeow_discuss/static/src/components/*.scss",
            "whatsmeow_discuss/static/src/components/*.xml",
        ],
    },
    "license": "LGPL-3",
    "installable": True,
    "application": False,
}
