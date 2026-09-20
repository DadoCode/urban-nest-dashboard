"""Reports: one generic structure, several renderings.

A report is {"title", "subtitle", "sections": [{"heading", "columns",
"rows", "note"?}]}. Columns are {"label", "fmt"}; rows are lists of *raw*
values (numbers stay numbers). build_* functions produce that structure
from kpis.py / the ledger -- no number is computed here that isn't
derived on read from the same data every page uses -- and to_csv/to_xlsx/
to_pdf/format_cell render it, so the on-screen preview, the spreadsheet
and the PDF can never disagree with each other."""
import csv
import io

import services.kpis as kpis
from services.common import MONTH_NAMES, get_properties, get_property, pct_delta

REPORT_TYPES = {
    "portfolio_monthly": "Portfolio monthly report",
    "property_monthly": "Property monthly report",
    "annual_portfolio": "Annual portfolio report",
    "expense": "Expense report",
}


# ---------------------------------------------------------------- formatting

def format_cell(value, fmt):
    if value is None or value == "":
        return "-"
    if fmt == "money":
        return f"£{value:,.0f}"
    if fmt == "money2":
        return f"£{value:,.2f}"
    if fmt == "pct":
        return f"{value:.0f}%"
    if fmt == "delta":
        return f"{value:+.1f}%"
    if fmt == "int":
        return f"{value:,.0f}"
    if fmt == "num1":
        return f"{value:,.1f}"
    return str(value)


def _col(label, fmt="text"):
    return {"label": label, "fmt": fmt}


# ------------------------------------------------------------------ sections

def _kpi_section(conn, property_id, start, end, pstart, pend, lystart, lyend, heading="Key figures"):
    cur = kpis.kpi_snapshot(conn, property_id, start, end)
    prev = kpis.kpi_snapshot(conn, property_id, pstart, pend)
    last = kpis.kpi_snapshot(conn, property_id, lystart, lyend)
    metrics = [
        ("Revenue", "revenue", "money", 100, 1),
        ("Costs", "costs", "money", 100, 1),
        ("Net profit", "net_profit", "money", 1000, 1),
        ("Margin", "margin", "pct", 0.05, 100),
        ("Occupancy", "occupancy", "pct", 0.05, 100),
        ("ADR", "adr", "money", 20, 1),
        ("RevPAR", "revpar", "money", 20, 1),
        ("Booked nights", "booked_nights", "int", 2, 1),
    ]
    rows = []
    for label, key, fmt, min_base, scale in metrics:
        rows.append([label, cur[key] * scale,
                     pct_delta(cur[key], prev[key], min_base=min_base),
                     pct_delta(cur[key], last[key], min_base=min_base)])
    # the value column has mixed formats per row, so the formatted string
    # is what's stored for display while the raw number stays in `raw`
    fmts = [m[2] for m in metrics]
    return {"heading": heading, "columns": [_col("Metric"), _col("Value", "mixed"),
                                             _col("vs prior period", "delta"), _col("vs same period last year", "delta")],
            "rows": rows, "row_fmts": fmts,
            "note": "Percentage changes are omitted when the comparison period's figure is too small to compare meaningfully."}


def _property_table(conn, start, end, heading="By property"):
    rows = []
    for p in get_properties(conn, include_overhead=False):
        s = kpis.kpi_snapshot(conn, p["id"], start, end)
        rows.append([p["name"], s["revenue"], s["costs"], s["net_profit"], s["margin"] * 100,
                     s["occupancy"] * 100, s["adr"]])
    rows.sort(key=lambda r: r[1], reverse=True)
    return {"heading": heading,
            "columns": [_col("Property"), _col("Revenue", "money"), _col("Costs", "money"), _col("Net profit", "money"),
                        _col("Margin", "pct"), _col("Occupancy", "pct"), _col("ADR", "money")],
            "rows": rows}


def _category_table(conn, start, end, property_id=None, heading="Expenses by category"):
    clause, params = ("AND property_id=?", (property_id,)) if property_id else ("", ())
    data = conn.execute(
        f"""SELECT category, SUM(amount) amt, COUNT(*) n FROM transactions
            WHERE direction='expense' AND category != 'reconciliation' AND date>=? AND date<? {clause}
            GROUP BY category ORDER BY amt DESC""",
        (start, end, *params),
    ).fetchall()
    total = sum(r["amt"] for r in data) or 0
    rows = [[r["category"], r["n"], r["amt"], (r["amt"] / total * 100) if total else None] for r in data]
    return {"heading": heading,
            "columns": [_col("Category"), _col("Items", "int"), _col("Amount", "money"), _col("Share", "pct")],
            "rows": rows}


def _ledger_table(conn, start, end, property_id=None, limit=1000, heading="Transactions"):
    clause, params = ("AND t.property_id=?", (property_id,)) if property_id else ("", ())
    data = conn.execute(
        f"""SELECT t.date, t.vendor, t.description, t.category, t.capex, t.direction, t.amount, p.name pname
            FROM transactions t JOIN properties p ON p.id = t.property_id
            WHERE t.date>=? AND t.date<? {clause} ORDER BY t.date, t.id LIMIT ?""",
        (start, end, *params, limit),
    ).fetchall()
    rows = []
    for r in data:
        kind = "Income" if r["direction"] == "income" else ("Capex" if r["capex"] else "Opex")
        cat = r["category"] + (" (historical adjustment)" if r["category"] == "reconciliation" else "")
        rows.append([r["date"], r["vendor"] or "", r["pname"], cat, kind, r["amount"]])
    return {"heading": heading,
            "columns": [_col("Date"), _col("Vendor"), _col("Property"), _col("Category"), _col("Type"), _col("Amount", "money2")],
            "rows": rows,
            "note": f"Showing up to {limit} transactions." if len(rows) >= limit else None}


def _vendor_table(conn, start, end, property_id=None, heading="Top vendors"):
    clause, params = ("AND t.property_id=?", (property_id,)) if property_id else ("", ())
    data = conn.execute(
        f"""SELECT v.name, SUM(t.amount) amt, COUNT(*) n FROM transactions t JOIN vendors v ON v.id = t.vendor_id
            WHERE t.direction='expense' AND t.category != 'reconciliation' AND t.date>=? AND t.date<? {clause}
            GROUP BY v.id ORDER BY amt DESC LIMIT 15""",
        (start, end, *params),
    ).fetchall()
    return {"heading": heading, "columns": [_col("Vendor"), _col("Items", "int"), _col("Amount", "money")],
            "rows": [[r["name"], r["n"], r["amt"]] for r in data]}


# ------------------------------------------------------------------- reports

def _month_ranges(year, month):
    start, end = kpis.month_bounds(year, month)
    p = kpis.prior_month(year, month)
    ly = (year - 1, month)
    return (start, end, *kpis.month_bounds(*p), *kpis.month_bounds(*ly))


def build_portfolio_monthly(conn, year, month):
    s, e, ps, pe, ls, le = _month_ranges(year, month)
    return {"title": "Portfolio monthly report", "subtitle": f"{MONTH_NAMES[month]} {year} · all properties",
            "sections": [_kpi_section(conn, None, s, e, ps, pe, ls, le),
                         _property_table(conn, s, e),
                         _category_table(conn, s, e)]}


def build_property_monthly(conn, property_id, year, month):
    prop = get_property(conn, property_id)
    s, e, ps, pe, ls, le = _month_ranges(year, month)
    sections = [_kpi_section(conn, property_id, s, e, ps, pe, ls, le),
                _category_table(conn, s, e, property_id),
                _vendor_table(conn, s, e, property_id),
                _ledger_table(conn, s, e, property_id, heading="Transactions this month")]
    return {"title": f"{prop['name']} — monthly report", "subtitle": f"{MONTH_NAMES[month]} {year}", "sections": sections}


def build_annual_portfolio(conn, year):
    s, e = kpis.range_bounds(year, 1, year, 12)
    ls, le = kpis.range_bounds(year - 1, 1, year - 1, 12)
    monthly = []
    for m in range(1, 13):
        ms, me = kpis.month_bounds(year, m)
        if f"{year}-{m:02d}" not in kpis.months_with_data(conn, None):
            continue
        snap = kpis.kpi_snapshot(conn, None, ms, me)
        monthly.append([MONTH_NAMES[m], snap["revenue"], snap["costs"], snap["net_profit"],
                        snap["margin"] * 100, snap["occupancy"] * 100])
    total = kpis.kpi_snapshot(conn, None, s, e)
    monthly.append(["Total / year average", total["revenue"], total["costs"], total["net_profit"],
                    total["margin"] * 100, total["occupancy"] * 100])
    last = kpis.kpi_snapshot(conn, None, ls, le)
    summary = {"heading": "Year summary",
               "columns": [_col("Metric"), _col("This year", "mixed"), _col("Last year", "mixed"), _col("Change", "delta")],
               "row_fmts": ["money", "money", "money", "pct", "pct"],
               "rows": [["Revenue", total["revenue"], last["revenue"], pct_delta(total["revenue"], last["revenue"], min_base=100)],
                        ["Costs", total["costs"], last["costs"], pct_delta(total["costs"], last["costs"], min_base=100)],
                        ["Net profit", total["net_profit"], last["net_profit"], pct_delta(total["net_profit"], last["net_profit"], min_base=1000)],
                        ["Margin", total["margin"] * 100, last["margin"] * 100, None],
                        ["Occupancy", total["occupancy"] * 100, last["occupancy"] * 100, None]]}
    return {"title": "Annual portfolio report", "subtitle": f"{year} · all properties",
            "sections": [summary,
                         {"heading": "Month by month",
                          "columns": [_col("Month"), _col("Revenue", "money"), _col("Costs", "money"), _col("Net profit", "money"),
                                      _col("Margin", "pct"), _col("Occupancy", "pct")], "rows": monthly},
                         _property_table(conn, s, e, heading=f"By property — {year}"),
                         _category_table(conn, s, e)]}


def build_expense(conn, from_ym, to_ym, property_id=None):
    fy, fm = map(int, from_ym.split("-"))
    ty, tm = map(int, to_ym.split("-"))
    s, e = kpis.range_bounds(fy, fm, ty, tm)
    scope = get_property(conn, property_id)["name"] if property_id else "all properties"
    label = f"{MONTH_NAMES[fm]} {fy}" if (fy, fm) == (ty, tm) else f"{MONTH_NAMES[fm]} {fy} to {MONTH_NAMES[tm]} {ty}"
    opex, capex = kpis.costs(conn, property_id, s, e, capex=False), kpis.costs(conn, property_id, s, e, capex=True)
    totals = {"heading": "Totals", "columns": [_col("Measure"), _col("Amount", "money")],
              "rows": [["Total costs", opex + capex], ["Opex", opex], ["Capex", capex]]}
    return {"title": "Expense report", "subtitle": f"{label} · {scope}",
            "sections": [totals, _category_table(conn, s, e, property_id), _vendor_table(conn, s, e, property_id),
                         _ledger_table(conn, s, e, property_id, heading="Transaction ledger")]}


# ------------------------------------------------------------------ renderers

def rows_for_display(section):
    """Formatted string rows (per-row formats when the column is 'mixed')."""
    out = []
    row_fmts = section.get("row_fmts")
    for i, row in enumerate(section["rows"]):
        cells = []
        for col, val in zip(section["columns"], row):
            fmt = row_fmts[i] if col["fmt"] == "mixed" and row_fmts else col["fmt"]
            cells.append(format_cell(val, fmt))
        out.append(cells)
    return out


def to_csv(report):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow([report["title"], report["subtitle"]])
    for sec in report["sections"]:
        w.writerow([])
        w.writerow([sec["heading"]])
        w.writerow([c["label"] for c in sec["columns"]])
        for row in sec["rows"]:
            w.writerow(["" if v is None else (round(v, 2) if isinstance(v, float) else v) for v in row])
    return buf.getvalue()


def to_xlsx(report):
    import openpyxl
    from openpyxl.styles import Font
    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    for i, sec in enumerate(report["sections"], 1):
        name = "".join(ch for ch in sec["heading"] if ch not in '[]:*?/\\')[:28] or f"Sheet{i}"
        ws = wb.create_sheet(f"{i}. {name}"[:31])
        ws.append([report["title"], report["subtitle"]])
        ws["A1"].font = Font(bold=True)
        ws.append([])
        ws.append([c["label"] for c in sec["columns"]])
        for cell in ws[3]:
            cell.font = Font(bold=True)
        for row in sec["rows"]:
            ws.append(["" if v is None else (round(v, 2) if isinstance(v, float) else v) for v in row])
        for col_cells in ws.columns:
            ws.column_dimensions[col_cells[0].column_letter].width = min(
                max(len(str(c.value)) if c.value is not None else 0 for c in col_cells) + 2, 48)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def to_pdf(report):
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    styles = getSampleStyleSheet()
    green = colors.HexColor("#056a4a")
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=16 * mm, rightMargin=16 * mm,
                            topMargin=16 * mm, bottomMargin=16 * mm, title=report["title"])
    story = [Paragraph(f"<b>Urban Nest Estates</b>", styles["Normal"]),
             Paragraph(report["title"], styles["Title"]),
             Paragraph(report["subtitle"], styles["Normal"]), Spacer(1, 8 * mm)]
    for sec in report["sections"]:
        story.append(Paragraph(sec["heading"], styles["Heading3"]))
        data = [[c["label"] for c in sec["columns"]]] + [
            [(cell[:38] + "…") if len(cell) > 39 else cell for cell in row] for row in rows_for_display(sec)]
        if len(data) == 1:
            story.append(Paragraph("No data for this period.", styles["Italic"]))
            story.append(Spacer(1, 4 * mm))
            continue
        table = Table(data, repeatRows=1, hAlign="LEFT")
        style = [("FONTSIZE", (0, 0), (-1, -1), 8), ("BACKGROUND", (0, 0), (-1, 0), green),
                 ("TEXTCOLOR", (0, 0), (-1, 0), colors.white), ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                 ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f3f6f4")]),
                 ("LINEBELOW", (0, 0), (-1, -1), 0.25, colors.HexColor("#d9e1dd")),
                 ("VALIGN", (0, 0), (-1, -1), "MIDDLE")]
        for ci, col in enumerate(sec["columns"]):
            if col["fmt"] not in ("text",):
                style.append(("ALIGN", (ci, 0), (ci, -1), "RIGHT"))
        table.setStyle(TableStyle(style))
        story.append(table)
        if sec.get("note"):
            story.append(Paragraph(f"<i>{sec['note']}</i>", styles["Normal"]))
        story.append(Spacer(1, 6 * mm))
    doc.build(story)
    return buf.getvalue()
