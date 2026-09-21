import re

from flask import Blueprint, Response, flash, redirect, render_template, request, url_for

import db
import services.kpis as kpis
import services.reporting as reporting
from services.context import request_context
from services.common import get_properties, get_property

bp = Blueprint("reports", __name__)

YM = re.compile(r"^\d{4}-\d{2}$")


def _build(conn, args):
    """Turns query params into a report dict, or (None, message) when the
    params don't describe a valid report."""
    rtype = args.get("type", "")
    cy, cm = kpis.current_period(conn)
    month = args.get("month") or f"{cy}-{cm:02d}"
    property_id = args.get("property") or None

    if rtype == "portfolio_monthly":
        if not YM.match(month):
            return None, "Pick a month for this report."
        y, m = map(int, month.split("-"))
        return reporting.build_portfolio_monthly(conn, y, m), None
    if rtype == "property_monthly":
        if not YM.match(month):
            return None, "Pick a month for this report."
        if not property_id or not get_property(conn, property_id):
            return None, "Pick a property for this report."
        y, m = map(int, month.split("-"))
        return reporting.build_property_monthly(conn, property_id, y, m), None
    if rtype == "annual_portfolio":
        year = args.get("year") or str(cy)
        if not year.isdigit() or len(year) != 4:
            return None, "Enter a four-digit year."
        return reporting.build_annual_portfolio(conn, int(year)), None
    if rtype == "expense":
        f, t = args.get("from") or f"{cy}-01", args.get("to") or f"{cy}-{cm:02d}"
        if not (YM.match(f) and YM.match(t)) or f > t:
            return None, "Pick a valid from/to month range (from must not be after to)."
        if property_id and not get_property(conn, property_id):
            return None, "That property doesn't exist."
        return reporting.build_expense(conn, f, t, property_id), None
    return None, "Choose a report type."


@bp.route("/reports")
def index():
    """The report forms start from the user's current period and property,
    but every field stays editable -- a report can cover any dates."""
    conn = db.get_conn()
    ctx = request_context(conn)
    return render_template(
        "reports/index.html", active="reports", all_properties=get_properties(conn), active_property=None,
        flats=get_properties(conn, include_overhead=False), report_types=reporting.REPORT_TYPES,
        default_month=ctx["to_input"], default_year=ctx["end_year"], default_from=ctx["from_input"],
        default_to=ctx["to_input"], default_property=ctx["property_id"] or "", period_display=ctx["display"],
    )


@bp.route("/reports/view")
def view():
    conn = db.get_conn()
    report, error = _build(conn, request.args)
    if error:
        flash(error, "error")
        return redirect(url_for("reports.index"))
    sections = [{**s, "display_rows": reporting.rows_for_display(s)} for s in report["sections"]]
    return render_template(
        "reports/view.html", active="reports", all_properties=get_properties(conn), active_property=None,
        report=report, sections=sections, params=request.args.to_dict(),
    )


@bp.route("/reports/export")
def export():
    conn = db.get_conn()
    report, error = _build(conn, request.args)
    if error:
        flash(error, "error")
        return redirect(url_for("reports.index"))
    fmt = request.args.get("format", "csv")
    name = re.sub(r"[^a-z0-9]+", "-", f"{report['title']} {report['subtitle']}".lower()).strip("-")[:80]
    if fmt == "xlsx":
        return Response(reporting.to_xlsx(report),
                        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        headers={"Content-Disposition": f"attachment; filename={name}.xlsx"})
    if fmt == "pdf":
        return Response(reporting.to_pdf(report), mimetype="application/pdf",
                        headers={"Content-Disposition": f"attachment; filename={name}.pdf"})
    return Response(reporting.to_csv(report), mimetype="text/csv",
                    headers={"Content-Disposition": f"attachment; filename={name}.csv"})
