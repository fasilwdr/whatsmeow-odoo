"""Start the warm-up ramp from a session's real history, not from today.

19.0.1.5.0 adds the per-session daily volume cap (PLAN.md §12.2), which is
enabled by default and grows from a day-1 allowance. A session that has been
sending for months is not on day one, and treating it as such would throttle a
working install down to ~20 messages a day overnight.

`sent_date` only exists from this version, so date the ramp from the oldest
outgoing message instead — the day the number actually started sending.
Sessions that have never sent keep an empty start date and are dated on their
first send, which is correct for them.
"""
import logging

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    cr.execute("""
        UPDATE whatsmeow_session s
           SET warmup_start_date = m.first_send
          FROM (SELECT session_id, min(create_date)::date AS first_send
                  FROM whatsmeow_message
                 WHERE direction = 'out' AND state != 'outgoing'
                 GROUP BY session_id) m
         WHERE m.session_id = s.id
           AND s.warmup_start_date IS NULL
    """)
    if cr.rowcount:
        _logger.info("whatsmeow: dated the warm-up ramp of %s session(s) from "
                     "their first outgoing message", cr.rowcount)
