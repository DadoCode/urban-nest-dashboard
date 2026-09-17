"""
Drops monthly_summary, expense_items and goals once
migrate_to_normalized_schema.py has verified the new tables (bookings,
transactions, targets) reconcile against them. Run this only after that
migration printed "verification OK".
"""
import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "dashboard.db"


def main():
    conn = sqlite3.connect(DB_PATH)
    for table in ("monthly_summary", "expense_items", "goals"):
        conn.execute(f"DROP TABLE IF EXISTS {table}")
        print(f"dropped {table}")
    conn.commit()
    conn.close()


if __name__ == "__main__":
    main()
