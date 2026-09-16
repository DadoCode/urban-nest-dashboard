"""
Imports the real Urban Nest Estates accounts tracker (an Excel workbook Faris
maintains by hand) into data/dashboard.db, a small SQLite database the Flask
app reads from.

Source layout (as of the 2025_6 workbook):
- One "<CODE><YY>" sheet per property per year (e.g. CC26, LW25) with a
  "Summary" table: one row per month with Net Profit, Operating Profit,
  Income, Total Costs, Opex, Capex, Occupancy, Days Booked. The property's
  full address is the sheet's title cell (B2).
- "Expense Breakdown26"/"Expense Breakdown25": itemized purchases (vendor,
  item, amount) in monthly column-triplets, stacked in sections per property
  -- only present for a few properties so far.
- "TARGETS": monthly income/profit targets and fixed-cost budget per
  property, for whichever properties have targets set.
- "Logins & Providers" holds account credentials -- deliberately never read.

Run again any time the workbook changes:
    python3 scripts/import_excel_tracker.py [path-to-xlsx]

This DROPS AND REBUILDS only the *_import-sourced rows (kind/source =
'excel_import'); anything added later through the app (new apartments,
uploaded-document expenses, manually entered goals) is left alone as long as
its property `code` still matches -- re-running the import is safe.
"""
import datetime
import re
import sqlite3
import sys
from pathlib import Path

import openpyxl

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "dashboard.db"
DEFAULT_XLSX = Path("/Users/Dado/Desktop/DADO - Biz Accounts Tracker 2025_6.xlsx")

MONTHS = [
    "january", "february", "march", "april", "may", "june",
    "july", "august", "september", "october", "november", "december",
]

# code -> (slug, display name) -- names taken verbatim from each sheet's own
# title cell so they match what Faris already calls each flat.
PROPERTIES = {
    "CC": ("crested-court", "40 Crested Court"),
    "LW": ("lascar-wharf", "602 Lascar Wharf"),
    "W8": ("campbell-hill-w8", "7A Campbell Hill"),
    "170E": ("170-miles-building", "170 Miles Building"),
    "175E": ("175-miles-building", "175 Miles Building"),
    "NW4": ("nw4", "Flat 3 NW4"),
    "TCR": ("tottenham-court-road", "Tottenham Court Road"),
    "11PW": ("11-perryfield-way", "11 Perryfield Way"),
    "19Draycott": ("19-draycott-ave", "19 Draycott Ave"),
    "S10": ("44-spooner-road", "44 Spooner Road"),
}

# The Main Page sheets' "Expenses" table runs straight into an unheadered,
# unlabeled block further down that just restates each property's own
# monthly total cost (Faris calls S10/44 Spooner Road "Sheffield" there,
# and "Lascar Wharf" is spelled "Lascar Warf") -- already captured exactly
# by that property's own Summary sheet, so these rows are skipped rather
# than double-counted as if they were shared overhead.
PROPERTY_ROW_ALIASES = {name.lower() for code, name in PROPERTIES.values()} | {
    code.lower() for code in PROPERTIES
} | {
    "sheffield", "lascar warf", "lascar wharf", "170", "175", "crested court",
    "campbell hill", "spooner road", "draycott ave", "draycott", "perryfield way",
}

# A pseudo-property for shared/overhead costs that aren't any one flat's --
# subscriptions, insurance, one-off business spend -- pulled from the Main
# Page sheets' "Expenses" table. Flagged is_overhead so the app can keep it
# out of the per-flat homepage grid.
OVERHEAD_PROPERTY = ("general-overheads", "Portfolio General Expenses")

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


def month_index(name):
    if not name:
        return None
    key = str(name).strip().lower()
    return MONTHS.index(key) + 1 if key in MONTHS else None


def find_header_row(ws, label, col, search_rows=60):
    for r in range(1, min(ws.max_row, search_rows) + 1):
        if str(ws.cell(row=r, column=col).value).strip().lower() == label:
            return r
    return None


def import_summary(conn, ws, prop_id, year):
    header_row = find_header_row(ws, "net profit", 3)  # column C
    if header_row is None:
        return 0
    count = 0
    r = header_row + 1
    while r <= ws.max_row:
        month_name = ws.cell(row=r, column=2).value
        mon = month_index(month_name)
        if mon is None:
            break  # hits "Running P/L..." or a blank row -- table's over
        vals = [ws.cell(row=r, column=c).value for c in range(3, 11)]
        vals = [v if isinstance(v, (int, float)) else None for v in vals]
        net_profit, operating_profit, income, total_costs, opex, capex, occupancy, days_booked = vals
        conn.execute(
            """INSERT INTO monthly_summary
               (property_id, year, month, income, total_costs, opex, capex,
                net_profit, operating_profit, occupancy, days_booked, source)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,'excel_import')
               ON CONFLICT(property_id, year, month) DO UPDATE SET
                 income=excluded.income, total_costs=excluded.total_costs,
                 opex=excluded.opex, capex=excluded.capex,
                 net_profit=excluded.net_profit, operating_profit=excluded.operating_profit,
                 occupancy=excluded.occupancy, days_booked=excluded.days_booked""",
            (prop_id, year, mon, income, total_costs, opex, capex,
             net_profit, operating_profit, occupancy, days_booked),
        )
        count += 1
        r += 1
    return count


def find_label_cells(ws, label, max_search_row=None, min_col=1):
    """All (row, col) where a cell's text matches `label` exactly (trimmed,
    case-insensitive) -- used to locate section headers like 'OPEX'.
    min_col matters here: the Summary table has its own plain 'Opex'/'Capex'
    *column* headers around column G/H, which would otherwise collide with
    the itemized breakdown's 'OPEX'/'CAPEX' *section title* further right."""
    hits = []
    max_r = max_search_row or ws.max_row
    for r in range(1, max_r + 1):
        for c in range(min_col, ws.max_column + 1):
            v = ws.cell(row=r, column=c).value
            if isinstance(v, str) and v.strip().lower() == label.lower():
                hits.append((r, c))
    return hits


def read_month_label_amount_rows(ws, header_row, end_row, year_for_col=None, default_year=None, min_col=1):
    """Reads a '<label> | <amount>' block laid out as repeating month-column
    pairs (label in col c, amount in col c+1, one pair per month) -- the
    shape used by each property sheet's OPEX/CAPEX/Bookings Income tables
    and by the Main Page's general-expenses table. Yields (year, month,
    label, amount), skipping blank-label rows (those are subtotal rows).
    min_col matters: a property sheet's own Summary table lives in columns
    2-10 and, purely by coincidence of row numbering, one of its month-name
    cells can land on the same row as a breakdown block's header -- without
    a floor on the column, that gets misread as one more month column."""
    month_cols = {}
    for c in range(min_col, ws.max_column + 1):
        mon = month_index(ws.cell(row=header_row, column=c).value)
        if mon:
            month_cols[c] = mon
    for r in range(header_row + 1, end_row + 1):
        for c, mon in month_cols.items():
            label = ws.cell(row=r, column=c).value
            amount = ws.cell(row=r, column=c + 1).value
            if isinstance(label, (datetime.date, datetime.datetime)):
                label = label.strftime("%Y-%m-%d")  # a Bookings Income row dated rather than named
            elif isinstance(label, (int, float)):
                label = str(int(label)) if float(label).is_integer() else str(label)  # e.g. a booking numbered "1" rather than named
            if not isinstance(label, str) or not label.strip():
                continue  # blank label = a subtotal row, not an item
            if not isinstance(amount, (int, float)):
                continue
            yr = (year_for_col or {}).get(c, default_year)
            if yr is None:
                continue
            yield yr, mon, label.strip(), amount


def import_property_breakdown(conn, ws, prop_id, year, code):
    """Per-property OPEX / CAPEX / Bookings Income itemized tables, sitting
    to the right of the Summary table on each property-year sheet. 2026
    sheets title these bare ('OPEX'); 2025 sheets prefix the property code
    ('CC OPEX') -- both are searched for."""
    sections = {
        "OPEX": "opex", f"{code} OPEX": "opex",
        "CAPEX": "capex", f"{code} CAPEX": "capex",
        "Bookings Income": "booking_income",
    }
    anchors = []
    for label, category in sections.items():
        for row, col in find_label_cells(ws, label, min_col=11):
            anchors.append((row, category))
    anchors = sorted(set(anchors))
    count = 0
    for i, (start_row, category) in enumerate(anchors):
        end_row = anchors[i + 1][0] - 1 if i + 1 < len(anchors) else ws.max_row
        header_row = find_header_row_any_col(ws, start_row, min(start_row + 10, ws.max_row))
        if header_row is None or header_row >= end_row:
            continue
        for yr, mon, label, amount in read_month_label_amount_rows(ws, header_row, end_row, default_year=year, min_col=11):
            conn.execute(
                """INSERT INTO expense_items (property_id, year, month, vendor, description, amount, category, source)
                   VALUES (?,?,?,NULL,?,?,?,'excel_import')""",
                (prop_id, yr, mon, label, amount, category),
            )
            count += 1
    return count


def find_header_row_any_col(ws, start_row, end_row):
    """Finds the month-name header row in [start_row, end_row] -- the row
    with the most cells that are month names, anywhere in the row (some
    properties' Bookings Income table starts mid-year, so it can't assume
    'January' is present)."""
    best_row, best_count = None, 0
    for r in range(start_row, end_row + 1):
        count = sum(1 for c in range(1, ws.max_column + 1) if month_index(ws.cell(row=r, column=c).value))
        if count > best_count:
            best_row, best_count = r, count
    return best_row if best_count >= 2 else None


def import_main_page_expenses(conn, wb, sheet_name, overhead_prop_id):
    """The Main Page's hand-kept 'Expenses' table: shared/overhead costs
    (subscriptions, insurance, one-off business spend) not tied to one
    flat, laid out as month-column label/amount pairs spanning a year
    boundary (a numeric year sits above the month row and applies to every
    column until the next year value appears)."""
    if sheet_name not in wb.sheetnames:
        return 0
    ws = wb[sheet_name]
    expenses_hits = find_label_cells(ws, "Expenses", max_search_row=5)
    income_hits = find_label_cells(ws, "Income")
    if not expenses_hits:
        return 0
    start_row = expenses_hits[0][0]
    end_row = income_hits[0][0] - 1 if income_hits else ws.max_row

    year_row = None
    for r in range(start_row, min(start_row + 6, end_row) + 1):
        if any(isinstance(ws.cell(row=r, column=c).value, (int, float)) and 2000 <= ws.cell(row=r, column=c).value <= 2100
               for c in range(1, ws.max_column + 1)):
            year_row = r
            break
    header_row = find_header_row_any_col(ws, start_row, end_row)
    if header_row is None:
        return 0

    year_for_col = {}
    if year_row is not None:
        current_year = None
        for c in range(1, ws.max_column + 1):
            v = ws.cell(row=year_row, column=c).value
            if isinstance(v, (int, float)) and 2000 <= v <= 2100:
                current_year = int(v)
            if current_year:
                year_for_col[c] = current_year

    count = 0
    for yr, mon, label, amount in read_month_label_amount_rows(ws, header_row, end_row, year_for_col=year_for_col):
        if label.strip().lower() in PROPERTY_ROW_ALIASES:
            continue  # a per-property cost restatement, not a shared overhead item
        conn.execute(
            """INSERT INTO expense_items (property_id, year, month, vendor, description, amount, category, source)
               VALUES (?,?,?,NULL,?,?,'overhead','excel_import')""",
            (overhead_prop_id, yr, mon, label, amount),
        )
        count += 1
    return count


def import_expense_breakdown_sheet(conn, wb, sheet_name, year, code_to_id):
    if sheet_name not in wb.sheetnames:
        return 0
    ws = wb[sheet_name]
    count = 0
    # Section headers look like "CC Expenses Breakdown" in column B.
    section_re = re.compile(r"^([A-Za-z0-9]+)\s+Expenses\s+Breakdown", re.I)
    sections = []
    for r in range(1, ws.max_row + 1):
        v = ws.cell(row=r, column=2).value
        if v and (m := section_re.match(str(v).strip())):
            sections.append((r, m.group(1).upper()))
    for i, (start_row, code) in enumerate(sections):
        prop_id = code_to_id.get(code)
        if prop_id is None:
            continue
        end_row = sections[i + 1][0] - 1 if i + 1 < len(sections) else ws.max_row
        # Month headers sit a few rows below the section title, one per
        # 3-column (vendor, item, amount) group.
        header_row = None
        for r in range(start_row, min(start_row + 8, end_row) + 1):
            if any(month_index(ws.cell(row=r, column=c).value) for c in range(2, ws.max_column + 1)):
                header_row = r
                break
        if header_row is None:
            continue
        month_cols = {}
        for c in range(2, ws.max_column + 1):
            mon = month_index(ws.cell(row=header_row, column=c).value)
            if mon:
                month_cols[c] = mon
        for r in range(header_row + 1, end_row + 1):
            for c, mon in month_cols.items():
                vendor = ws.cell(row=r, column=c).value
                desc = ws.cell(row=r, column=c + 1).value
                amount = ws.cell(row=r, column=c + 2).value
                if isinstance(vendor, str) and vendor.strip().upper() in ("OPEX", "CAPEX"):
                    continue  # a monthly subtotal row, not a line item
                if not isinstance(amount, (int, float)) or (not vendor and not desc):
                    continue
                conn.execute(
                    """INSERT INTO expense_items
                       (property_id, year, month, vendor, description, amount, category, source)
                       VALUES (?,?,?,?,?,?,'purchase','excel_import')""",
                    (prop_id, year, mon, vendor, desc, amount),
                )
                count += 1
    return count


def import_targets(conn, wb, code_to_id):
    if "TARGETS" not in wb.sheetnames:
        return 0
    ws = wb["TARGETS"]
    count = 0
    for r in range(1, ws.max_row + 1):
        code_cell = ws.cell(row=r, column=2).value
        if code_cell is None or str(code_cell).strip() == "":
            continue
        if isinstance(code_cell, (int, float)):
            code = str(int(code_cell))
        else:
            code = str(code_cell).strip().upper()
        # TARGETS labels some properties with a bare number ("170", "175")
        # instead of their code -- map those back.
        code = {"170": "170E", "175": "175E"}.get(code, code)
        if code not in code_to_id:
            continue
        header_row = r + 1
        if str(ws.cell(row=header_row, column=2).value).strip().lower() != "months":
            continue
        prop_id = code_to_id[code]
        rr = header_row + 1
        while rr <= ws.max_row:
            mon = month_index(ws.cell(row=rr, column=2).value)
            if mon is None:
                break
            vals = [ws.cell(row=rr, column=c).value for c in range(3, 13)]
            vals = [v if isinstance(v, (int, float)) else None for v in vals]
            _profit25, _income25, profit_target, income_target, min_target, rent, bills, ctax, cleaning, other = vals
            conn.execute(
                """INSERT INTO goals
                   (property_id, year, month, income_target, profit_target, min_target,
                    rent, bills, council_tax, cleaning, other, source)
                   VALUES (?,2026,?,?,?,?,?,?,?,?,?,'excel_import')
                   ON CONFLICT(property_id, year, month) DO UPDATE SET
                     income_target=excluded.income_target, profit_target=excluded.profit_target,
                     min_target=excluded.min_target, rent=excluded.rent, bills=excluded.bills,
                     council_tax=excluded.council_tax, cleaning=excluded.cleaning, other=excluded.other""",
                (prop_id, mon, income_target, profit_target, min_target, rent, bills, ctax, cleaning, other),
            )
            count += 1
            rr += 1
    return count


def main():
    xlsx_path = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_XLSX
    if not xlsx_path.exists():
        sys.exit(f"Can't find the workbook at {xlsx_path}")

    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(SCHEMA)

    wb = openpyxl.load_workbook(xlsx_path, data_only=True)

    code_to_id = {}
    for code, (slug, name) in PROPERTIES.items():
        conn.execute(
            """INSERT INTO properties (id, code, name, address)
               VALUES (?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET name=excluded.name""",
            (slug, code, name, name),
        )
        code_to_id[code] = slug

    conn.execute(
        """INSERT INTO properties (id, code, name, address, is_overhead)
           VALUES (?,?,?,?,1)
           ON CONFLICT(id) DO UPDATE SET name=excluded.name""",
        (OVERHEAD_PROPERTY[0], "OVERHEAD", OVERHEAD_PROPERTY[1], "Shared across the portfolio -- not one flat"),
    )

    # Wipe everything we (re-)derive from the workbook before re-inserting,
    # so a re-run never leaves stale rows behind.
    conn.execute("DELETE FROM expense_items WHERE source = 'excel_import'")
    conn.execute("DELETE FROM goals WHERE source = 'excel_import'")

    summary_rows = 0
    breakdown_rows = 0
    for code in PROPERTIES:
        for year, suffix in ((2025, "25"), (2026, "26")):
            sheet_name = f"{code}{suffix}"
            if sheet_name in wb.sheetnames:
                summary_rows += import_summary(conn, wb[sheet_name], code_to_id[code], year)
                breakdown_rows += import_property_breakdown(conn, wb[sheet_name], code_to_id[code], year, code)

    expense_rows = 0
    expense_rows += import_expense_breakdown_sheet(conn, wb, "Expense Breakdown26", 2026, code_to_id)
    expense_rows += import_expense_breakdown_sheet(conn, wb, "Expense Breakdown25", 2025, code_to_id)

    overhead_rows = 0
    overhead_rows += import_main_page_expenses(conn, wb, "Main Page26", OVERHEAD_PROPERTY[0])
    overhead_rows += import_main_page_expenses(conn, wb, "Main25", OVERHEAD_PROPERTY[0])

    goal_rows = import_targets(conn, wb, code_to_id)

    conn.commit()
    conn.close()
    print(f"Imported {len(PROPERTIES)} properties, {summary_rows} monthly summary rows, "
          f"{breakdown_rows} opex/capex/booking line items, {expense_rows} itemized purchase-detail rows, "
          f"{overhead_rows} shared-overhead line items, {goal_rows} monthly goal rows -> {DB_PATH}")


if __name__ == "__main__":
    main()
