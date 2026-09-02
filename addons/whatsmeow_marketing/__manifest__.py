{
    "name": "Whatsmeow Marketing",
    "version": "19.0.1.0.0",
    "summary": "Mass WhatsApp campaigns: broadcast lists, traces, /stop-/start, replies in Discuss",
    "description": """
Send a WhatsApp message to many contacts at once, the way Email Marketing sends a mailing.

Recipients come from a Broadcast List (a marketing-only contact book, convertible to real
contacts), from a domain over res.partner, or from a saved Dynamic List. One body is
rendered per recipient, delivered / read / replied / failed are tracked per recipient, and
a contact who writes "/stop" is never included in a campaign again until they write
"/start".

Nothing here talks to a gateway. Campaigns feed the core paced queue in small batches, so
every send inherits the warm-up ramp, the daily and hourly volume caps, the pacing between
messages and the idempotency key — which is what keeps a mass send from getting the number
banned.
""",
    "author": "Fasil, Bytesraw",
    "category": "Marketing",
    "depends": ["whatsmeow", "whatsmeow_template", "whatsmeow_discuss", "mail", "web"],
    "data": [
        "security/whatsmeow_marketing_groups.xml",
        "security/ir.model.access.csv",
        "security/whatsmeow_marketing_rules.xml",
        "views/whatsmeow_broadcast_contact_views.xml",
        "views/whatsmeow_broadcast_list_views.xml",
        "views/whatsmeow_broadcast_import_views.xml",
        "views/whatsmeow_marketing_filter_views.xml",
        "views/whatsmeow_marketing_trace_views.xml",
        "views/whatsmeow_marketing_campaign_views.xml",
        "views/whatsmeow_session_views.xml",
        "views/res_partner_views.xml",
        "views/menus.xml",
        "data/ir_cron.xml",
    ],
    "license": "LGPL-3",
    "installable": True,
    "application": True,
}
