"""
One-time migration from the old cache-table shape (monthly_summary,
expense_items, goals) to the normalized shape (bookings, transactions,
targets, property_fixed_costs) that dashboard/kpis.py derives everything
from on read.

Design choices, and why:

- `transactions.amount` keeps its original sign (a refund can be negative);
  `direction` ('income'/'expense') says which bucket it belongs to. Summing
  a category's transactions by direction is all `kpis.py` ever needs to do.
- Individual historical "Bookings Income" line items (free-text labels like
  "15-18", "check in", a bare ISO date) don't reliably carry real
  check-in/check-out dates -- rather than guess, they become `transactions`
  (direction='income', category='booking_income'), and ONE synthetic
  `bookings` row per property/month carries the real `days_booked` figure
  from the old monthly_summary so occupancy/ADR/RevPAR still derive
  correctly through the exact same code path real reservations will use
  going forward (new uploads/iCal syncs populate `bookings` for real).
- Every property/month that had a trusted (non-placeholder) monthly_summary
  total gets a `category='reconciliation'` transaction for whatever the
  summed line items don't already account for -- so
  SUM(transactions by direction) always equals the original Excel total
  exactly, without pretending the line-item detail is more precise than it
  actually is.

Safe to inspect before committing: writes to data/dashboard.db but first
copies it to data/dashboard.db.pre_migration_backup. Run again only after
restoring that backup -- it does not delete the old tables until the
migrated totals have been verified to reconcile (see the check at the end).
"""
import datetime
import re
import shutil
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "dashboard.db"
BACKUP_PATH = ROOT / "data" / "dashboard.db.pre_migration_backup"

sys.path.insert(0, str(ROOT / "dashboard"))
import db  # noqa: E402  (adds the new tables via ensure_schema)

OPEX_LABEL_CATEGORY = [
    (re.compile(r"clean", re.I), "cleaning"),
    (re.compile(r"water|electric|heating|wifi|gas\b", re.I), "utilities"),
    (re.compile(r"council tax", re.I), "council_tax"),
    (re.compile(r"mngmt|management fee", re.I), "management_fee"),
    (re.compile(r"purchase", re.I), "purchase"),
]

DAY_RANGE_RE = re.compile(r"^\s*(\d{1,2})\s*-\s*(\d{1,2})\b")


def categorize_opex_label(label):
    for pattern, category in OPEX_LABEL_CATEGORY:
        if pattern.search(label or ""):
            return category
    return "other"


def migrate_properties(conn):
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(properties)")}
    if "is_overhead" in cols:
        conn.execute("UPDATE properties SET type = CASE WHEN is_overhead = 1 THEN 'overhead' ELSE 'flat' END")
    print(f"properties: {conn.execute('SELECT COUNT(*) FROM properties').fetchone()[0]} rows, type column set")


def migrate_targets(conn):
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='goals'").fetchone():
        return 0
    conn.execute("DELETE FROM targets")
    rows = conn.execute("SELECT * FROM goals").fetchall()
    for r in rows:
        conn.execute(
            """INSERT INTO targets (property_id, year, month, revenue_target, profit_target, occupancy_target, source)
               VALUES (?,?,?,?,?,NULL,?)
               ON CONFLICT(property_id, year, month) DO UPDATE SET
                 revenue_target=excluded.revenue_target, profit_target=excluded.profit_target""",
            (r["property_id"], r["year"], r["month"], r["income_target"], r["profit_target"], r["source"]),
        )
    print(f"targets: migrated {len(rows)} rows from goals")
    return len(rows)


def migrate_fixed_costs(conn):
    """One representative snapshot per property from goals' rent/bills/
    council_tax/cleaning/other -- these were fairly constant month to
    month in the source (a recurring budget, not an actuals figure)."""
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='goals'").fetchone():
        return 0
    conn.execute("DELETE FROM property_fixed_costs")
    latest_per_property = conn.execute(
        """SELECT g.* FROM goals g
           JOIN (SELECT property_id, MAX(year*100+month) latest FROM goals GROUP BY property_id) m
             ON m.property_id = g.property_id AND g.year*100+g.month = m.latest"""
    ).fetchall()
    count = 0
    for r in latest_per_property:
        for label, amount in [("rent", r["rent"]), ("council_tax", r["council_tax"]),
                               ("cleaning_baseline", r["cleaning"]), ("bills", r["bills"]), ("other", r["other"])]:
            if amount:
                conn.execute(
                    """INSERT INTO property_fixed_costs (property_id, label, amount, is_percentage, effective_from)
                       VALUES (?,?,?,0,?)""",
                    (r["property_id"], label, amount, f"{r['year']}-{r['month']:02d}-01"),
                )
                count += 1
    print(f"property_fixed_costs: seeded {count} rows")
    return count


def migrate_expense_items(conn):
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='expense_items'").fetchone():
        return 0
    conn.execute("DELETE FROM transactions WHERE source = 'excel_import'")
    rows = conn.execute("SELECT * FROM expense_items").fetchall()
    for r in rows:
        date = f"{r['year']}-{r['month']:02d}-01"
        if r["category"] == "booking_income":
            conn.execute(
                """INSERT INTO transactions (property_id, date, vendor, description, amount, direction, category, capex, source, document_id)
                   VALUES (?,?,?,?,?,'income','booking_income',0,?,?)""",
                (r["property_id"], date, r["vendor"], r["description"], r["amount"], r["source"], r["document_id"]),
            )
        elif r["category"] == "capex":
            category = categorize_opex_label(r["description"] or r["vendor"] or "")
            conn.execute(
                """INSERT INTO transactions (property_id, date, vendor, description, amount, direction, category, capex, source, document_id)
                   VALUES (?,?,?,?,?,'expense',?,1,?,?)""",
                (r["property_id"], date, r["vendor"], r["description"], r["amount"],
                 category if category != "other" else "capex", r["source"], r["document_id"]),
            )
        elif r["category"] == "opex":
            category = categorize_opex_label(r["description"] or r["vendor"] or "")
            conn.execute(
                """INSERT INTO transactions (property_id, date, vendor, description, amount, direction, category, capex, source, document_id)
                   VALUES (?,?,?,?,?,'expense',?,0,?,?)""",
                (r["property_id"], date, r["vendor"], r["description"], r["amount"], category, r["source"], r["document_id"]),
            )
        else:  # purchase | overhead | cleaning | utilities | other -- already a fine category
            conn.execute(
                """INSERT INTO transactions (property_id, date, vendor, description, amount, direction, category, capex, source, document_id)
                   VALUES (?,?,?,?,?,'expense',?,0,?,?)""",
                (r["property_id"], date, r["vendor"], r["description"], r["amount"], r["category"], r["source"], r["document_id"]),
            )
    print(f"transactions: migrated {len(rows)} rows from expense_items")
    return len(rows)


def migrate_monthly_summary(conn):
    """Adds the reconciliation transactions and the one synthetic
    occupancy-carrying booking per property/month -- see module docstring."""
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='monthly_summary'").fetchone():
        return 0, 0
    conn.execute("DELETE FROM bookings WHERE source = 'excel_import'")
    conn.execute("DELETE FROM transactions WHERE source = 'excel_import' AND category = 'reconciliation'")

    rows = conn.execute(
        "SELECT * FROM monthly_summary WHERE source = 'excel_import' AND (income IS NOT NULL OR total_costs IS NOT NULL)"
    ).fetchall()
    recon_count = 0
    booking_count = 0
    for r in rows:
        if not r["income"] and not r["total_costs"]:
            continue  # a zeroed-out placeholder month -- nothing real happened, nothing to reconcile
        year, month, pid = r["year"], r["month"], r["property_id"]
        month_end = f"{year}-{month+1:02d}-01" if month < 12 else f"{year+1}-01-01"
        derived = conn.execute(
            """SELECT
                 COALESCE(SUM(amount) FILTER (WHERE direction='income'), 0) inc,
                 COALESCE(SUM(amount) FILTER (WHERE direction='expense'), 0) exp
               FROM transactions WHERE property_id=? AND date >= ? AND date < ?""",
            (pid, f"{year}-{month:02d}-01", month_end),
        ).fetchone()
        target_income = r["income"] or 0
        target_costs = abs(r["total_costs"] or 0)
        recon_income = round(target_income - derived["inc"], 2)
        recon_costs = round(target_costs - derived["exp"], 2)
        date = f"{year}-{month:02d}-01"
        if abs(recon_income) > 0.01:
            conn.execute(
                """INSERT INTO transactions (property_id, date, description, amount, direction, category, source)
                   VALUES (?,?,'Excel summary reconciliation adjustment',?,'income','reconciliation','excel_import')""",
                (pid, date, recon_income),
            )
            recon_count += 1
        if abs(recon_costs) > 0.01:
            conn.execute(
                """INSERT INTO transactions (property_id, date, description, amount, direction, category, source)
                   VALUES (?,?,'Excel summary reconciliation adjustment',?,'expense','reconciliation','excel_import')""",
                (pid, date, recon_costs),
            )
            recon_count += 1
        if r["days_booked"]:
            check_in = datetime.date(year, month, 1)
            check_out = check_in + datetime.timedelta(days=int(r["days_booked"]))
            conn.execute(
                """INSERT INTO bookings (property_id, platform, reservation_id, check_in, check_out,
                                          gross_revenue, platform_fees, cleaning_fee, net_revenue, status, source)
                   VALUES (?,NULL,'monthly-aggregate',?,?,0,0,0,0,'confirmed','excel_import')""",
                (pid, check_in.isoformat(), check_out.isoformat()),
            )
            booking_count += 1
    print(f"reconciliation transactions: {recon_count}; synthetic occupancy bookings: {booking_count}")
    return recon_count, booking_count


def verify(conn):
    """Every property/month that had a real monthly_summary income figure
    must now derive to the exact same number from bookings+transactions."""
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='monthly_summary'").fetchone():
        return True
    rows = conn.execute(
        "SELECT * FROM monthly_summary WHERE source='excel_import' AND income IS NOT NULL AND income != 0"
    ).fetchall()
    bad = []
    for r in rows:
        year, month, pid = r["year"], r["month"], r["property_id"]
        end = f"{year}-{month+1:02d}-01" if month < 12 else f"{year+1}-01-01"
        derived_income = conn.execute(
            "SELECT COALESCE(SUM(amount),0) FROM transactions WHERE property_id=? AND direction='income' AND date>=? AND date<?",
            (pid, f"{year}-{month:02d}-01", end),
        ).fetchone()[0]
        if abs(derived_income - r["income"]) > 0.02:
            bad.append((pid, year, month, derived_income, r["income"]))
    if bad:
        print(f"VERIFICATION FAILED for {len(bad)} property/months:")
        for b in bad[:10]:
            print(" ", b)
        return False
    print(f"verification OK: {len(rows)} property/months reconcile exactly")
    return True


def main():
    if not DB_PATH.exists():
        sys.exit(f"No database at {DB_PATH} -- nothing to migrate.")
    shutil.copy(DB_PATH, BACKUP_PATH)
    print(f"Backed up {DB_PATH} -> {BACKUP_PATH}")

    db.ensure_schema()
    conn = db.get_conn()

    migrate_properties(conn)
    migrate_targets(conn)
    migrate_fixed_costs(conn)
    migrate_expense_items(conn)
    migrate_monthly_summary(conn)
    conn.commit()

    ok = verify(conn)
    conn.close()

    if not ok:
        print("\nNot dropping old tables -- fix the migration and re-run (it's idempotent, safe to repeat).")
        sys.exit(1)

    print("\nMigration verified. Old tables (monthly_summary, expense_items, goals) are still present")
    print("for safety -- drop them with scripts/drop_legacy_tables.py once app.py no longer reads them.")


if __name__ == "__main__":
    main()
