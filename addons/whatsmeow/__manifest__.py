{
    "name": "Whatsmeow WhatsApp Connector",
    "version": "19.0.1.5.0",
    "summary": "Send/receive WhatsApp via self-hosted whatsmeow gateways",
    "description": """
Drive one or more self-hosted whatsmeow gateways from Odoo.

Each whatsmeow.connection record is one gateway endpoint; each whatsmeow.session
is one WhatsApp number paired by QR. Inbound messages arrive on a webhook and are
logged as whatsmeow.message records and posted to the matching partner's chatter.
""",
    "author": "Fasil, Bytesraw",
    "category": "Discuss",
    "depends": ["base", "mail"],
    "data": [
        "security/whatsmeow_groups.xml",
        "security/ir.model.access.csv",
        "views/res_partner_views.xml",
        "views/whatsmeow_connection_views.xml",
        "views/whatsmeow_session_views.xml",
        "views/whatsmeow_message_views.xml",
        "views/menus.xml",
        "data/ir_cron.xml",
    ],
    "license": "LGPL-3",
    "installable": True,
    "application": True,
}
