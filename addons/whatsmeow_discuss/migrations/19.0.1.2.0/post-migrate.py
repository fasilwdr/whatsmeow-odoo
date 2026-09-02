"""Re-label conversations that were named before the number was part of the name.

19.0.1.2.0 titles a conversation "Name (+number)" instead of the name alone, so
an operator can tell three customers with the same first name apart and check
the number against the contact record without leaving Discuss. New
conversations get it for free; existing ones keep whatever they were called.

Only labels *we* generated are rewritten — a channel whose name still equals its
correspondent's name, or the bare number, is one nobody has touched. Anything
else is somebody's deliberate choice and is left alone, which is the same rule
`_wa_refresh_channel_identity` follows at runtime.

Both the number and the name come from the newest inbound message rather than
the contact record. The contact may carry a landline or a formatted number,
while the message carries the digits WhatsApp actually delivered from — and a
LID-only conversation has no contact at all, only the push name it was named
after in the first place.
"""
import logging

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    cr.execute("""
        WITH conv AS (
            SELECT DISTINCT ON (c.id)
                   c.id                AS channel_id,
                   c.name              AS old_name,
                   coalesce(nullif(p.name, ''), m.push_name) AS display_name,
                   regexp_replace(m.phone, '\\D', '', 'g')    AS digits
              FROM discuss_channel c
              JOIN whatsmeow_message m
                ON m.session_id = c.whatsmeow_session_id
               AND coalesce(m.chat_jid, '') = coalesce(c.whatsmeow_chat_jid, '')
               AND m.direction = 'in'
         LEFT JOIN res_partner p ON p.id = c.whatsmeow_partner_id
             WHERE c.channel_type = 'whatsmeow'
               AND m.chat_type = 'private'
               AND coalesce(m.phone, '') <> ''
          ORDER BY c.id, m.id DESC
        )
        UPDATE discuss_channel c
           SET name = conv.display_name || ' (+' || conv.digits || ')'
          FROM conv
         WHERE c.id = conv.channel_id
           AND coalesce(conv.display_name, '') <> ''
           -- a contact auto-named after its own number is not a name
           AND regexp_replace(conv.display_name, '\\D', '', 'g') <> conv.digits
           -- only a label this module generated: the name alone, or the number
           AND conv.old_name IN (conv.display_name, conv.digits, '+' || conv.digits)
    """)
    if cr.rowcount:
        _logger.info("whatsmeow_discuss: re-labelled %s conversation(s) with "
                     "their contact's number", cr.rowcount)
