{
    "name": "Whatsmeow Templates & Composer",
    "version": "19.0.1.0.0",
    "summary": "Send templated WhatsApp messages from a record of any model",
    "description": """
Send WhatsApp from any Odoo record.

A whatsmeow.template is a stored message body — rendered against the record with
{{ object.name }} placeholders — optionally carrying static attachments or a
generated PDF report. Operators launch it from the chatter's WhatsApp button, from
the Action menu, or automation fires it through a "Send WhatsApp" server action.

Unlike Meta's Cloud API there is no template approval and no 24-hour session
window, so a template here is simply a saved, field-interpolated message. Sending
reuses the core queue untouched: the composer only creates outgoing
whatsmeow.message rows, which inherit pacing, retries and idempotency for free.
""",
    "author": "Fasil",
    "category": "Discuss",
    "depends": ["whatsmeow", "mail"],
    "data": [
        "security/ir.model.access.csv",
        "views/whatsmeow_template_views.xml",
        "views/whatsmeow_composer_views.xml",
        "views/ir_actions_server_views.xml",
        "views/menus.xml",
    ],
    "assets": {
        "web.assets_backend": [
            "whatsmeow_template/static/src/chatter_patch.js",
            "whatsmeow_template/static/src/chatter_patch.xml",
        ],
    },
    "license": "LGPL-3",
    "installable": True,
    "application": False,
}
