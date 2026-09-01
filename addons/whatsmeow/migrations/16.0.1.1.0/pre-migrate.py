"""Drop duplicate inbound messages so the new unique index can be created.

Until 16.0.1.1.0 the webhook deduped with a search-then-create, which cannot
hold against the gateway's concurrent retries, and WhatsApp itself delivers
some messages twice — the first copy empty, the second real. Both left two
rows sharing one wa_message_id, which the unique index would now refuse.

Keep the copy worth keeping: the real one over the empty stand-in, and the
later one when neither is a stand-in (that's the order they arrive in). The
stand-in's body is matched by text because these rows predate the
`is_placeholder` flag that replaces the guesswork going forward.
"""
import logging

_logger = logging.getLogger(__name__)

PLACEHOLDER_LIKE = "[unsupported message type:%"


def migrate(cr, version):
    cr.execute(
        """
        SELECT wa_message_id,
               (array_agg(id ORDER BY
                    (COALESCE(body, '') LIKE %s) ASC,  -- real copies first
                    id DESC                            -- then the latest
               ))[1] AS keep_id,
               count(*) AS n
          FROM whatsmeow_message
         WHERE direction = 'in' AND wa_message_id IS NOT NULL
         GROUP BY wa_message_id
        HAVING count(*) > 1
        """,
        (PLACEHOLDER_LIKE,),
    )
    groups = cr.fetchall()
    if not groups:
        return

    keep_ids = [row[1] for row in groups]
    cr.execute(
        """
        SELECT id FROM whatsmeow_message
         WHERE direction = 'in'
           AND wa_message_id IN %s
           AND id != ALL(%s)
        """,
        (tuple(row[0] for row in groups), keep_ids),
    )
    doomed = [row[0] for row in cr.fetchall()]
    if not doomed:
        return

    # Attachments are pointed at the row by res_id, not by a foreign key, so
    # they would outlive it as orphans in the filestore.
    cr.execute(
        """
        DELETE FROM ir_attachment
         WHERE res_model = 'whatsmeow.message' AND res_id = ANY(%s)
        """,
        (doomed,),
    )
    cr.execute("DELETE FROM whatsmeow_message WHERE id = ANY(%s)", (doomed,))
    _logger.info(
        "whatsmeow: removed %s duplicate inbound message(s) across %s "
        "wa_message_id(s) before adding the unique index",
        len(doomed), len(groups),
    )
