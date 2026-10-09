"""Which property-months are controlled by an applied monthly workbook import.

The monthly workbook is the primary financial source; a month it has imported for a property is "workbook-controlled". Anything written
into such a month outside the workbook (a manual expense, a confirmed document line) makes the dashboard stop matching the workbook, so
the UI asks before allowing it. Read-only helpers; nothing here writes."""
import json


def workbook_batch_for(conn, property_id, ym):
    """The id of the applied workbook import that controls this property in month `ym` ('YYYY-MM'), or None."""
    for b in conn.execute("SELECT id, properties FROM import_batches WHERE kind='workbook' AND status='applied' AND period=? ORDER BY id DESC", (ym,)):
        if property_id in json.loads(b["properties"] or "[]"):
            return b["id"]
    row = conn.execute(
        """SELECT b.id FROM transactions t JOIN import_batches b ON b.id=t.import_batch_id
           WHERE t.property_id=? AND substr(t.date,1,7)=? AND b.status='applied' AND b.kind='workbook' ORDER BY b.id DESC LIMIT 1""",
        (property_id, ym)).fetchone()
    return row["id"] if row else None


def controlled_months(conn, property_id):
    """{'YYYY-MM': batch_id} for every month of this property that a workbook import controls (newest batch wins)."""
    out = {}
    for b in conn.execute("SELECT id, period, properties FROM import_batches WHERE kind='workbook' AND status='applied' AND period IS NOT NULL ORDER BY id"):
        if property_id in json.loads(b["properties"] or "[]"):
            out[b["period"]] = b["id"]
    for r in conn.execute(
            """SELECT substr(t.date,1,7) ym, MAX(b.id) bid FROM transactions t JOIN import_batches b ON b.id=t.import_batch_id
               WHERE t.property_id=? AND b.status='applied' AND b.kind='workbook' GROUP BY ym""", (property_id,)):
        out[r["ym"]] = max(out.get(r["ym"], 0), r["bid"])
    return out
