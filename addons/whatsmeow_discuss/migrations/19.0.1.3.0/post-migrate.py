"""Give existing Discuss bubbles the "Name (+number)" label new ones get.

19.0.1.3.0 names the sender above an inbound bubble the way the conversation
itself is titled. New bubbles are labelled as they are posted; without this the
history of every open conversation would keep showing the name alone, and a
thread would change how it names the same person half-way down.

The rule is `whatsmeow.message._wa_sender_label`, restated in SQL: the contact's
name, else the WhatsApp push name, together with the number the message was
delivered from — and no label at all when either half is missing, or when the
"name" is only that number over again (an auto-created contact).

The name is read from the contact as it is today. A bubble posted from now on
keeps the name it was posted under; for history, today's name is the best
answer there is.
"""
import logging

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    cr.execute("""
        WITH sender AS (
            SELECT m.mail_message_id                                  AS message_id,
                   btrim(coalesce(nullif(p.name, ''), m.push_name, '')) AS name,
                   btrim(m.phone)                                     AS phone
              FROM whatsmeow_message m
         LEFT JOIN res_partner p ON p.id = m.partner_id
             WHERE m.direction = 'in'
               AND m.mail_message_id IS NOT NULL
               AND btrim(coalesce(m.phone, '')) <> ''
        )
        UPDATE mail_message mm
           SET whatsmeow_author_label = sender.name || ' (' ||
               CASE WHEN sender.phone LIKE '+%%' THEN sender.phone
                    ELSE '+' || sender.phone END || ')'
          FROM sender
         WHERE mm.id = sender.message_id
           AND mm.model = 'discuss.channel'
           AND mm.whatsmeow_author_label IS NULL
           AND sender.name <> ''
           -- a contact auto-named after its own number is not a name
           AND NOT (
               regexp_replace(sender.name, '\\D', '', 'g') <> ''
               AND right(regexp_replace(sender.name, '\\D', '', 'g'), 10)
                 = right(regexp_replace(sender.phone, '\\D', '', 'g'), 10)
           )
    """)
    if cr.rowcount:
        _logger.info("whatsmeow_discuss: labelled %s existing bubble(s) with "
                     "their sender's number", cr.rowcount)
