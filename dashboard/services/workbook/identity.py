"""Who is who: sheet codes, names and aliases -> one stable property id.

Matching priority (never fuzzy, never auto-creating):
  1. the exact property id
  2. an exact known alias (same text)
  3. a normalised alias (case, spacing and punctuation ignored)
  4. the workbook sheet code mapping
  5. otherwise None -> a person confirms
"""
import re

from . import config as C


def norm(text):
    return re.sub(r"[^a-z0-9]+", "", (text or "").lower())


def add_alias(conn, property_id, text, kind="name"):
    """Record `text` as a name for the property. Returns True if it was new; an alias that already
    belongs to ANOTHER property is left alone (and reported by returning None)."""
    key = norm(text)
    if not key:
        return False
    row = conn.execute("SELECT property_id FROM property_identity_aliases WHERE alias_norm=?", (key,)).fetchone()
    if row:
        return False if row["property_id"] == property_id else None
    conn.execute("INSERT INTO property_identity_aliases (alias_norm, alias_text, property_id, kind) VALUES (?,?,?,?)",
                 (key, text.strip(), property_id, kind))
    return True


def seed(conn):
    """Idempotent: for every configured property that already exists, store its sheet code, names and aliases."""
    for code, (pid, name) in C.PROPERTY_SHEETS.items():
        row = conn.execute("SELECT name FROM properties WHERE id=?", (pid,)).fetchone()
        if not row:
            continue
        conn.execute("INSERT OR IGNORE INTO workbook_sheet_map (sheet_code, property_id, source) VALUES (?,?,'seed')", (code, pid))
        add_alias(conn, pid, code, "code")
        add_alias(conn, pid, name, "name")
        add_alias(conn, pid, row["name"], "name")
        for alias in C.PROPERTY_ALIASES.get(code, []):
            add_alias(conn, pid, alias, "name")


def mapping(conn):
    """{sheet_code: {"pid", "name", "exists"}} -- the database first, then the built-in list for properties
    that are mapped but not (yet) in the dashboard."""
    out = {}
    for r in conn.execute("""SELECT m.sheet_code, m.property_id, p.name FROM workbook_sheet_map m JOIN properties p ON p.id=m.property_id"""):
        out[r["sheet_code"]] = {"pid": r["property_id"], "name": r["name"], "exists": True}
    for code, (pid, name) in C.PROPERTY_SHEETS.items():
        if code not in out:
            exists = bool(conn.execute("SELECT 1 FROM properties WHERE id=?", (pid,)).fetchone())
            out[code] = {"pid": pid, "name": name, "exists": exists}
    return out


def resolve(conn, text):
    """-> (property_id, how) or (None, None)."""
    if not text:
        return None, None
    text = text.strip()
    if conn.execute("SELECT 1 FROM properties WHERE id=?", (text,)).fetchone():
        return text, "id"
    row = conn.execute("SELECT property_id FROM property_identity_aliases WHERE alias_text=?", (text,)).fetchone()
    if row:
        return row["property_id"], "alias"
    row = conn.execute("SELECT property_id FROM property_identity_aliases WHERE alias_norm=?", (norm(text),)).fetchone()
    if row:
        return row["property_id"], "normalised alias"
    row = conn.execute("SELECT property_id FROM workbook_sheet_map WHERE sheet_code=? COLLATE NOCASE", (text,)).fetchone()
    if row:
        return row["property_id"], "sheet"
    return None, None


def aliases_of(conn, property_id):
    return [dict(r) for r in conn.execute(
        "SELECT alias_text, kind FROM property_identity_aliases WHERE property_id=? ORDER BY kind, alias_text", (property_id,))]


def sheet_codes_of(conn, property_id):
    return [r["sheet_code"] for r in conn.execute("SELECT sheet_code FROM workbook_sheet_map WHERE property_id=? ORDER BY sheet_code", (property_id,))]
