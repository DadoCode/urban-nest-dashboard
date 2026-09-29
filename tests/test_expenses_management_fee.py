"""Regression test: a management-fee transaction can never be counted in
an Expenses total, in any scope. This is the exact bug found and fixed
in Phase 3's first pass -- category='management_fee' has
direction='expense' (it's money leaving the FLAT's own book), so a
generic "sum every expense transaction" query sweeps it in; but that
same amount is Urban Nest's own income (Urban Nest Revenue /
Management Fee Earned), so counting it again here would double it.

Self-contained: builds a minimal in-memory schema (not the live
database, so this can't silently pass just because today's data
happens to look right) with the worked example from the brief --
managed property booking revenue £3,000, 15% fee = £450 -- plus one
genuine, unrelated property expense and one genuine business expense,
and checks the fee appears in neither.

Run with:  python3 tests/test_expenses_management_fee.py
"""
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "dashboard"))

import db  # noqa: E402
from routes.expenses import _costs  # noqa: E402

conn = sqlite3.connect(":memory:")
conn.row_factory = sqlite3.Row
conn.executescript(db.SCHEMA)

conn.executemany(
    "INSERT INTO properties (id, name, type) VALUES (?,?,?)",
    [
        ("managed-flat", "Test Managed Flat", "flat"),
        ("overhead", "Business Overheads", "overhead"),
    ],
)

# The worked example: £3,000 booking revenue, 15% fee = £450, recorded
# as an expense transaction on the managed flat's own book (mirrors how
# the real Excel import records it -- see the Phase 3 audit).
MANAGEMENT_FEE = 450.0
conn.execute(
    """INSERT INTO transactions (property_id, date, category, direction, amount, capex, source)
       VALUES ('managed-flat', '2026-06-01', 'management_fee', 'expense', ?, 0, 'excel_import')""",
    (MANAGEMENT_FEE,),
)
# A genuine, unrelated property cost on the same flat -- must still count.
GENUINE_PROPERTY_COST = 120.0
conn.execute(
    """INSERT INTO transactions (property_id, date, category, direction, amount, capex, source)
       VALUES ('managed-flat', '2026-06-01', 'cleaning', 'expense', ?, 0, 'excel_import')""",
    (GENUINE_PROPERTY_COST,),
)
# A genuine business cost -- must still count, and must never be
# conflated with the fee just because both are "expense" rows.
GENUINE_BUSINESS_COST = 80.0
conn.execute(
    """INSERT INTO transactions (property_id, date, category, direction, amount, capex, source)
       VALUES ('overhead', '2026-06-01', 'software', 'expense', ?, 0, 'excel_import')""",
    (GENUINE_BUSINESS_COST,),
)
conn.commit()

start, end = "2026-06-01", "2026-07-01"
failures = []


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f" -- {detail}" if detail and not cond else ""))
    if not cond:
        failures.append(name)


property_total = _costs(conn, start, end, scope="property")
business_total = _costs(conn, start, end, scope="business")
all_total = _costs(conn, start, end, scope="all")
single_prop = _costs(conn, start, end, property_id="managed-flat")

check("management fee excluded from Property Costs",
      property_total == GENUINE_PROPERTY_COST,
      f"got {property_total}, expected {GENUINE_PROPERTY_COST} (fee={MANAGEMENT_FEE} must not be included)")
check("management fee excluded from Business Costs",
      business_total == GENUINE_BUSINESS_COST,
      f"got {business_total}, expected {GENUINE_BUSINESS_COST}")
check("management fee excluded from All / Total Recorded Costs",
      all_total == GENUINE_PROPERTY_COST + GENUINE_BUSINESS_COST,
      f"got {all_total}, expected {GENUINE_PROPERTY_COST + GENUINE_BUSINESS_COST}")
check("management fee excluded from a single-property view",
      single_prop == GENUINE_PROPERTY_COST,
      f"got {single_prop}, expected {GENUINE_PROPERTY_COST}")
check("Property + Business = Total invariant still holds",
      property_total + business_total == all_total)
check("genuine property cost is NOT accidentally dropped too",
      property_total > 0)
check("genuine business cost is NOT accidentally dropped too",
      business_total > 0)

# Opex/Capex split must also exclude it (capex=0 on the fee row, so a
# naive "opex = everything not capex" would still sweep it in).
opex_total = _costs(conn, start, end, scope="all", capex=False)
check("management fee excluded from Opex too",
      opex_total == GENUINE_PROPERTY_COST + GENUINE_BUSINESS_COST,
      f"got {opex_total}")

print()
if failures:
    print(f"{len(failures)} FAILED: {', '.join(failures)}")
    sys.exit(1)
print("All management-fee exclusion checks passed.")
