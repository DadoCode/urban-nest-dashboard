"""Resolves free-text vendor strings (from manual entry or document
extraction) to a stable vendors.id -- exact match on trimmed text, same
rule the one-time backfill in db.py uses. Not fuzzy matching; "Amazon" and
"AMAZON.CO.UK" stay distinct vendors until someone merges them by hand."""


def get_or_create_vendor(conn, name):
    name = (name or "").strip()
    if not name:
        return None
    row = conn.execute("SELECT id FROM vendors WHERE name = ?", (name,)).fetchone()
    if row:
        return row["id"]
    return conn.execute("INSERT INTO vendors (name) VALUES (?)", (name,)).lastrowid
