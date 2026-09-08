"""Carry each step's and each option's single server action into the new
many-to-many.

Post- rather than pre-migrate because the relation tables are created by the
schema update in between: a pre-migrate has the old column and nowhere to put
it. The old column survives the update — Odoo never drops a column for a field
that has gone away — so it is still there to read, and this drops it once its
contents are safe.
"""

# (model table, relation table, the column naming the owning record)
MOVES = (
    ("whatsmeow_bot_step", "whatsmeow_bot_step_action_rel", "step_id"),
    ("whatsmeow_bot_answer", "whatsmeow_bot_answer_action_rel", "answer_id"),
)


def migrate(cr, version):
    for table, relation, column in MOVES:
        cr.execute("""
            SELECT 1 FROM information_schema.columns
             WHERE table_name = %s AND column_name = 'server_action_id'
        """, (table,))
        if not cr.fetchone():
            continue  # a fresh install, or this has already run
        # The table and column names are literals above, never input; the
        # relation's primary key is (owner, action), so a re-run is a no-op
        # rather than a duplicate.
        cr.execute("""
            INSERT INTO {relation} ({column}, action_id)
                 SELECT id, server_action_id FROM {table}
                  WHERE server_action_id IS NOT NULL
            ON CONFLICT DO NOTHING
        """.format(relation=relation, column=column, table=table))
        cr.execute("ALTER TABLE {table} DROP COLUMN server_action_id".format(
            table=table))
