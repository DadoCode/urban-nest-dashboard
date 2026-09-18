"""SQLite access layer for the business dashboard. Thin wrappers only --
routes in app.py own the request/response handling.

Normalized schema (see scripts/migrate_to_normalized_schema.py for the
one-time move from the older monthly_summary/expense_items/goals shape):
KPIs are never stored as totals -- dashboard/kpis.py derives them on read
from `bookings` (reservation-level income) and `transactions` (everything
else, income or expense) for whatever property/date-range is asked for."""
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "dashboard.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS properties (
    id TEXT PRIMARY KEY,
    code TEXT UNIQUE,
    name TEXT NOT NULL,
    address TEXT,
    active INTEGER NOT NULL DEFAULT 1,
    type TEXT NOT NULL DEFAULT 'flat',   -- 'flat' | 'overhead'
    start_date TEXT,
    ical_url TEXT,
    ical_synced_at TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS property_fixed_costs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    property_id TEXT NOT NULL REFERENCES properties(id),
    label TEXT NOT NULL,          -- 'rent' | 'council_tax' | 'management_fee_pct' | ...
    amount REAL NOT NULL,
    is_percentage INTEGER NOT NULL DEFAULT 0,
    effective_from TEXT,
    effective_to TEXT
);

CREATE TABLE IF NOT EXISTS targets (
    property_id TEXT NOT NULL REFERENCES properties(id),
    year INTEGER NOT NULL,
    month INTEGER NOT NULL,
    revenue_target REAL, profit_target REAL, occupancy_target REAL,
    source TEXT NOT NULL DEFAULT 'excel_import',
    PRIMARY KEY (property_id, year, month)
);

CREATE TABLE IF NOT EXISTS bookings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    property_id TEXT NOT NULL REFERENCES properties(id),
    platform TEXT,                 -- 'airbnb' | 'booking_com' | 'direct' | 'other'
    reservation_id TEXT,
    check_in TEXT NOT NULL,        -- ISO date
    check_out TEXT NOT NULL,       -- ISO date, exclusive (last night is check_out - 1 day)
    gross_revenue REAL NOT NULL DEFAULT 0,
    platform_fees REAL NOT NULL DEFAULT 0,
    cleaning_fee REAL NOT NULL DEFAULT 0,
    net_revenue REAL NOT NULL,
    status TEXT NOT NULL DEFAULT 'confirmed',   -- 'confirmed' | 'cancelled'
    source TEXT NOT NULL DEFAULT 'excel_import',
    document_id INTEGER REFERENCES documents(id)
);

CREATE TABLE IF NOT EXISTS transactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    property_id TEXT NOT NULL REFERENCES properties(id),
    date TEXT NOT NULL,            -- ISO date (best available; 1st-of-month if only month known)
    vendor TEXT, description TEXT,
    amount REAL NOT NULL,          -- always a positive magnitude
    direction TEXT NOT NULL DEFAULT 'expense',  -- 'income' | 'expense'
    category TEXT NOT NULL DEFAULT 'other',
    capex INTEGER NOT NULL DEFAULT 0,
    source TEXT NOT NULL DEFAULT 'excel_import',
    document_id INTEGER REFERENCES documents(id),
    recurring_cost_id INTEGER REFERENCES property_fixed_costs(id),
    edited_at TEXT, edited_by TEXT
);

CREATE TABLE IF NOT EXISTS documents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    property_id TEXT REFERENCES properties(id),
    filename TEXT NOT NULL,
    stored_path TEXT NOT NULL,
    doc_type TEXT,
    detected_year INTEGER, detected_month INTEGER,
    status TEXT NOT NULL DEFAULT 'pending',   -- pending|extracting|extracted|reviewed|confirmed|failed
    confidence REAL,
    reviewed INTEGER NOT NULL DEFAULT 0,
    extracted_json TEXT,
    uploaded_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- One row per extracted/reviewed line item on a document -- the explicit
-- lineage step between a source file and a confirmed ledger entry:
-- Document -> document_items (extracted) -> document_items (reviewed) ->
-- transactions/bookings (confirmed) -> kpis.py (derived). Not yet written
-- to by app.py (Phase 2 adds the schema; Phase 6 wires the review page to
-- it) -- documents.extracted_json remains the source of truth until then.
CREATE TABLE IF NOT EXISTS document_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id INTEGER NOT NULL REFERENCES documents(id),
    line_index INTEGER NOT NULL,
    raw_description TEXT,
    date TEXT, vendor TEXT, amount REAL,
    direction TEXT,                -- 'income' | 'expense'
    property_id TEXT REFERENCES properties(id),
    category TEXT, capex INTEGER NOT NULL DEFAULT 0,
    confidence REAL,
    duplicate_of INTEGER REFERENCES transactions(id),
    include INTEGER NOT NULL DEFAULT 1,
    reviewed INTEGER NOT NULL DEFAULT 0,
    original_extracted_value TEXT,   -- JSON snapshot of the raw extraction for this line
    final_value TEXT                 -- JSON snapshot as confirmed, once reviewed
);

-- What documents each property is expected to produce each month, so
-- "data completeness" is judged against that property's own real sources
-- rather than one hardcoded list. Not yet read by app.py (Phase 2 adds the
-- schema and seeds sensible defaults at Add-Property time; Phase 4 wires
-- the Overview completeness card to it).
CREATE TABLE IF NOT EXISTS property_data_requirements (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    property_id TEXT NOT NULL REFERENCES properties(id),
    source_type TEXT NOT NULL,     -- matches documents.doc_type
    required INTEGER NOT NULL DEFAULT 1,
    effective_from TEXT, effective_to TEXT
);

-- What changed on a financial record and what it used to say -- separate
-- from transactions.edited_at/edited_by (which only says *that* a row was
-- edited). Not yet written to by app.py (Phase 2 adds the schema; Phase 5
-- routes every transaction edit through here).
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_type TEXT NOT NULL,     -- 'transaction' | 'booking' | 'document_item'
    entity_id INTEGER NOT NULL,
    action TEXT NOT NULL,          -- 'create' | 'edit' | 'delete'
    field TEXT, old_value TEXT, new_value TEXT,
    timestamp TEXT NOT NULL DEFAULT (datetime('now')),
    user TEXT
);

CREATE INDEX IF NOT EXISTS idx_bookings_property_date ON bookings(property_id, check_in);
CREATE INDEX IF NOT EXISTS idx_transactions_property_date ON transactions(property_id, date);
CREATE INDEX IF NOT EXISTS idx_document_items_document ON document_items(document_id);
CREATE INDEX IF NOT EXISTS idx_data_requirements_property ON property_data_requirements(property_id);
CREATE INDEX IF NOT EXISTS idx_audit_log_entity ON audit_log(entity_type, entity_id);
"""


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def ensure_schema():
    conn = get_conn()
    conn.executescript(SCHEMA)
    existing_cols = {row["name"] for row in conn.execute("PRAGMA table_info(properties)")}
    for col, ddl in [
        ("ical_url", "TEXT"), ("ical_synced_at", "TEXT"),
        ("type", "TEXT NOT NULL DEFAULT 'flat'"), ("start_date", "TEXT"),
    ]:
        if col not in existing_cols:
            conn.execute(f"ALTER TABLE properties ADD COLUMN {col} {ddl}")
    tx_cols = {row["name"] for row in conn.execute("PRAGMA table_info(transactions)")}
    for col, ddl in [
        ("recurring_cost_id", "INTEGER REFERENCES property_fixed_costs(id)"),
        ("edited_at", "TEXT"), ("edited_by", "TEXT"),
    ]:
        if col not in tx_cols:
            conn.execute(f"ALTER TABLE transactions ADD COLUMN {col} {ddl}")

    doc_info = list(conn.execute("PRAGMA table_info(documents)"))
    doc_cols = {row["name"] for row in doc_info}
    for col, ddl in [
        ("detected_year", "INTEGER"), ("detected_month", "INTEGER"),
        ("confidence", "REAL"), ("reviewed", "INTEGER NOT NULL DEFAULT 0"),
    ]:
        if col not in doc_cols:
            conn.execute(f"ALTER TABLE documents ADD COLUMN {col} {ddl}")

    property_id_not_null = any(r["name"] == "property_id" and r["notnull"] for r in doc_info)
    if property_id_not_null:
        # The Document Inbox needs to hold a file before a flat is known --
        # rebuild the table with property_id nullable, keeping every row.
        # legacy_alter_table=ON matters here: without it, RENAME silently
        # rewrites *other* tables' `REFERENCES documents(id)` to point at
        # the temporary documents_old name, which then breaks the moment
        # that table is dropped (a classic SQLite foreign-key gotcha).
        conn.executescript("""
            PRAGMA legacy_alter_table = ON;
            ALTER TABLE documents RENAME TO documents_old;
            CREATE TABLE documents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                property_id TEXT REFERENCES properties(id),
                filename TEXT NOT NULL,
                stored_path TEXT NOT NULL,
                doc_type TEXT,
                detected_year INTEGER, detected_month INTEGER,
                status TEXT NOT NULL DEFAULT 'pending',
                confidence REAL,
                reviewed INTEGER NOT NULL DEFAULT 0,
                extracted_json TEXT,
                uploaded_at TEXT NOT NULL DEFAULT (datetime('now'))
            );
            INSERT INTO documents SELECT id, property_id, filename, stored_path, doc_type,
                detected_year, detected_month, status, confidence, reviewed, extracted_json, uploaded_at
                FROM documents_old;
            DROP TABLE documents_old;
        """)
    conn.commit()
    conn.close()


def slugify(text):
    import re
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug or "flat"


def unique_slug(conn, base):
    slug = base
    n = 2
    while conn.execute("SELECT 1 FROM properties WHERE id = ?", (slug,)).fetchone():
        slug = f"{base}-{n}"
        n += 1
    return slug
