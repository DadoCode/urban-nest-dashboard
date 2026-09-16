"""SQLite access layer for the business dashboard. Thin wrappers only --
routes in app.py own the request/response handling."""
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
    is_overhead INTEGER NOT NULL DEFAULT 0,
    ical_url TEXT,
    ical_synced_at TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS monthly_summary (
    property_id TEXT NOT NULL REFERENCES properties(id),
    year INTEGER NOT NULL,
    month INTEGER NOT NULL,
    income REAL, total_costs REAL, opex REAL, capex REAL,
    net_profit REAL, operating_profit REAL,
    occupancy REAL, days_booked REAL,
    source TEXT NOT NULL DEFAULT 'excel_import',
    PRIMARY KEY (property_id, year, month)
);

CREATE TABLE IF NOT EXISTS goals (
    property_id TEXT NOT NULL REFERENCES properties(id),
    year INTEGER NOT NULL,
    month INTEGER NOT NULL,
    income_target REAL, profit_target REAL, min_target REAL,
    rent REAL, bills REAL, council_tax REAL, cleaning REAL, other REAL,
    source TEXT NOT NULL DEFAULT 'excel_import',
    PRIMARY KEY (property_id, year, month)
);

CREATE TABLE IF NOT EXISTS expense_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    property_id TEXT NOT NULL REFERENCES properties(id),
    year INTEGER NOT NULL,
    month INTEGER NOT NULL,
    vendor TEXT, description TEXT, amount REAL NOT NULL,
    category TEXT NOT NULL DEFAULT 'purchase',
    source TEXT NOT NULL DEFAULT 'excel_import',
    document_id INTEGER REFERENCES documents(id)
);

CREATE TABLE IF NOT EXISTS documents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    property_id TEXT NOT NULL REFERENCES properties(id),
    filename TEXT NOT NULL,
    stored_path TEXT NOT NULL,
    doc_type TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    extracted_json TEXT,
    uploaded_at TEXT NOT NULL DEFAULT (datetime('now'))
);
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
    for col, ddl in [("ical_url", "TEXT"), ("ical_synced_at", "TEXT")]:
        if col not in existing_cols:
            conn.execute(f"ALTER TABLE properties ADD COLUMN {col} {ddl}")
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
