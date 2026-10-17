{
    "name": "Whatsmeow Point of Sale",
    "version": "20.0.1.0.0",
    "summary": "Send POS receipts and invoices over WhatsApp, and pair a number from the register",
    "description": """
Send the customer their receipt — and their invoice, when the order was
invoiced — over WhatsApp, from the same Send Receipt dialog that emails it.

Each point of sale sends from a whatsmeow session, picked automatically from the
connected numbers when none is set. When no number is connected but a gateway
is, a manager can pair one by scanning a QR code without leaving the register.

The message text comes from a whatsmeow template (one for pos.order, one for
account.move); the defaults shipped here are used when none is chosen. Sending
goes through the core whatsmeow.message, so opt-outs, chatter logging and the
delivery receipts all apply exactly as for any other send.
""",
    "author": "Fasil, Bytesraw",
    "category": "Sales/Point of Sale",
    "depends": ["point_of_sale", "whatsmeow_template"],
    "data": [
        "data/whatsmeow_template_data.xml",
        "views/res_config_settings_views.xml",
    ],
    "assets": {
        "point_of_sale._assets_pos": [
            "whatsmeow_pos/static/src/**/*",
        ],
        "web.assets_tests": [
            "whatsmeow_pos/static/tests/tours/**/*",
        ],
    },
    "license": "LGPL-3",
    "installable": True,
    "application": False,
}
