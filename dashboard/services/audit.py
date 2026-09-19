"""Writes to audit_log -- what changed on a financial record and what it
used to say. transactions.edited_at/edited_by (set alongside this) answers
"was this row touched"; audit_log answers "what did it say before"."""
import datetime


def record(conn, entity_type, entity_id, action, field=None, old_value=None, new_value=None, user=None):
    conn.execute(
        """INSERT INTO audit_log (entity_type, entity_id, action, field, old_value, new_value, user)
           VALUES (?,?,?,?,?,?,?)""",
        (entity_type, entity_id, action, field,
         None if old_value is None else str(old_value),
         None if new_value is None else str(new_value), user),
    )


def record_edits(conn, entity_type, entity_id, old_row, new_values, user=None):
    """Diffs old_row (a sqlite3.Row) against new_values (a dict of the
    fields being set) and writes one audit_log row per changed field --
    only the fields that actually changed, not a blanket "edited" stamp."""
    changed = False
    for field, new_value in new_values.items():
        old_value = old_row[field] if field in old_row.keys() else None
        if str(old_value) != str(new_value):
            record(conn, entity_type, entity_id, "edit", field, old_value, new_value, user)
            changed = True
    return changed
