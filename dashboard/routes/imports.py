"""Import monthly workbook: upload -> validate -> preview one month -> apply -> (undo).

The workbook is prepared elsewhere from the raw documents; this blueprint only
imports that one predictable structure, safely. Nothing is written until a
person presses Apply on a preview of exactly what would change.
"""
import hashlib
import json

from flask import Blueprint, flash, redirect, render_template, request, url_for

import db
from services import runtime
import services.reconcile as rc
from services.common import short_name, get_properties, is_managed
from services.provenance import current_rate_context
from services.workbook import apply as A
from services.workbook import batches as B
from services.workbook import config as C
from services.workbook import plan as P
from services.workbook import reader

bp = Blueprint("imports", __name__)

MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"]


def month_label(ym):
    return f"{MONTHS[int(ym[5:7]) - 1]} {ym[:4]}"


def _valid_month(ym):
    return bool(ym) and len(ym) == 7 and ym[4] == "-" and ym[:4].isdigit() and ym[5:].isdigit() and 1 <= int(ym[5:]) <= 12


def _choices(source):
    """What the person typed or ticked for new properties and excluded rows: ({pid: {name, model, pct}}, excluded set | None)."""
    confirm = {}
    for key in source:
        if key.startswith("new_model:"):
            pid = key.split(":", 1)[1]
            raw = (source.get(f"new_pct:{pid}") or "").strip()
            try:
                pct = max(0.0, min(100.0, float(raw))) if raw else None
            except ValueError:
                pct = None
            confirm[pid] = {"name": (source.get(f"new_name:{pid}") or "").strip(), "model": source.get(key) or "", "pct": pct}
    excluded = set(source.getlist("exclude")) if "exclude_form" in source else None
    distinct = set(source.getlist("distinct")) if "exclude_form" in source else None
    return confirm, excluded, distinct


def fingerprint(plan):
    """What the preview showed, so Apply can refuse if the ledger moved underneath it."""
    body = [(i["property_id"], i["status"], i.get("counts"), i.get("days")) for i in plan["properties"] + [plan["business"]] if i]
    return hashlib.sha256(json.dumps(body, sort_keys=True, default=str).encode()).hexdigest()[:16]


@bp.route("/imports")
def index():
    conn = db.get_conn()
    status = request.args.get("status") or ""
    sort = request.args.get("sort") or "uploaded"
    direction = "asc" if request.args.get("dir") == "asc" else "desc"
    if status not in ("", "staged", "applied", "undone"):
        status = ""
    rows = B.list_batches(conn, status, sort, direction)
    names = {p["id"]: p["name"] for p in get_properties(conn)}
    for r in rows:
        ids = json.loads(r["properties"] or "[]")
        r["property_names"] = [short_name(names.get(i, "Business costs" if i == P.BUSINESS_ID else i)) for i in ids]
        r["period_label"] = month_label(r["period"]) if r["period"] else "—"
        # what kind of batch it is, and a file label that keeps the part that tells imports apart (the tail), not the identical prefix
        is_cleanup = (r["filename"] or "").startswith("cleanup:")
        stem = (r["filename"] or "").rsplit(".", 1)[0].replace("_", " ")
        r["kind_label"] = "Clean-up" if is_cleanup else "Workbook"
        r["file_label"] = (r["filename"].split(":", 1)[1].strip().capitalize() if is_cleanup else (stem if len(stem) <= 34 else "…" + stem[-33:]))
    return render_template("imports.html", active="documents", all_properties=get_properties(conn), active_property=None,
                           batches=rows, f_status=status, sort=sort, direction=direction, recon_total=len(rc.candidates(conn)))


@bp.route("/imports/upload", methods=["POST"])
def upload():
    f = request.files.get("workbook")
    if not f or not f.filename:
        flash("No file was chosen. Choose the monthly .xlsx workbook.", "error")
        return redirect(url_for("imports.index"))
    if not f.filename.lower().endswith((".xlsx", ".xlsm")):
        flash("That is not an Excel workbook. Choose the monthly .xlsx workbook.", "error")
        return redirect(url_for("imports.index"))
    data = f.read()
    if len(data) > runtime.max_upload_bytes():
        flash("That file is larger than the upload limit. Choose the monthly .xlsx workbook, without extra attachments or images.", "error")
        return redirect(url_for("imports.index"))
    conn = db.get_conn()
    try:
        batch_id = B.stage(conn, data, f.filename)
    except reader.WorkbookError as exc:
        flash(str(exc), "error")
        return redirect(url_for("imports.index"))
    return redirect(url_for("imports.batch", batch_id=batch_id))


def _default_month(overview, parsed):
    """The month being reported: the latest where at least half the properties have income (the same idea as the
    dashboard's own "current period"), so a few placeholder entries in later months don't hijack the default."""
    current = [m["ym"] for m in overview if m["of"] and m["properties_with_income"] * 2 >= m["of"]]
    if current:
        return current[-1]
    return overview[-1]["ym"] if overview else None


@bp.route("/imports/<int:batch_id>")
def batch(batch_id):
    conn = db.get_conn()
    row, parsed = B.load(conn, batch_id)
    if not row:
        flash("We couldn't find that import.", "error")
        return redirect(url_for("imports.index"))
    ctx = {"active": "documents", "all_properties": get_properties(conn), "active_property": None, "batch": dict(row),
           "month_label": month_label, "business_id": P.BUSINESS_ID}
    if row["status"] in ("applied", "undone"):
        return _applied(conn, row, parsed, ctx)
    report = json.loads(row["validation"] or "{}")
    overview = P.month_overview(conn, parsed) if report.get("can_import") else []
    ym = request.args.get("month") if _valid_month(request.args.get("month")) else _default_month(overview, parsed)
    confirm, excluded, distinct = _choices(request.args)
    plan = P.plan_month(conn, parsed, ym, excluded=excluded, confirm=confirm, distinct=distinct) if (ym and report.get("can_import")) else None
    base = P.plan_month(conn, parsed, ym, with_after=False) if plan else None
    month_issues = [i for i in parsed["issues"] if i["level"] in ("error", "review") and i["month"] == ym]
    elsewhere = sum(1 for i in parsed["issues"] if i["level"] in ("error", "review") and i["month"] not in (None, ym))
    # "Last imported": the newest applied workbook batch for that month that actually covered the property (undone batches do not count)
    last_imported = {}
    if ym:
        for b in conn.execute("SELECT id, applied_at, properties FROM import_batches WHERE kind='workbook' AND status='applied' AND period=? ORDER BY applied_at DESC, id DESC", (ym,)):
            for pid in json.loads(b["properties"] or "[]"):
                last_imported.setdefault(pid, {"batch": b["id"], "date": (b["applied_at"] or "")[:10]})
    return render_template("import_batch.html", report=report, overview=overview, ym=ym, plan=plan, month_issues=month_issues,
                           elsewhere=elsewhere, fingerprint=fingerprint(base) if base else "", last_imported=last_imported, **ctx)


def _applied(conn, row, parsed, ctx):
    names = {p["id"]: p["name"] for p in get_properties(conn)}
    names[P.BUSINESS_ID] = "Business costs"
    before = json.loads(row["before_totals"] or "{}")
    after = json.loads(row["after_totals"] or "{}")
    # An applied import is an audit record: the stored import-time results are shown exactly as evaluated then (verdicts included). Where the
    # property's fee % has changed since, a separate "current configuration" note is attached to that check; it never replaces the result.
    recon_at_import = json.loads(row["reconciliation"] or "{}")
    recon = recon_at_import
    current = {pid: current_rate_context(conn, pid, checks) for pid, checks in recon_at_import.items()}
    verification = []
    if row["status"] == "applied" and parsed:
        for code, info in P.identity_map(conn, parsed).items():
            if info["pid"] in after and (code in parsed["properties"] or info.get("main_only")) and info["exists"]:
                v = A.verify_item(conn, parsed, code, row["period"])
                checks = recon.get(info["pid"], [])
                flagged = [c for c in checks if c["status"] == "REVIEW" or (c["status"] == "NO CONTROL" and c["imported"] not in (0, 0.0, None))]
                bad = [r for r in v["rows"] if r["gating"]]
                v["verdict"] = "REVIEW" if (flagged or bad) else "PASS"
                v["reasons"] = [c["metric"] for c in flagged]
                verification.append(v)
    created = json.loads(row["new_properties"] or "[]")
    return render_template("import_applied.html", names=names, before=before, after=after, recon=recon, current=current, verification=verification, managed_ids={p["id"] for p in get_properties(conn) if is_managed(p)},
                           created=created, undoable=(row["status"] == "applied"), **ctx)


@bp.route("/imports/<int:batch_id>/apply", methods=["POST"])
def apply(batch_id):
    conn = db.get_conn()
    row, parsed = B.load(conn, batch_id)
    ym = request.form.get("month")
    if not row or row["status"] != "staged" or not _valid_month(ym):
        flash("That import can't be applied (it may already have been applied or cancelled).", "error")
        return redirect(url_for("imports.index"))
    base = P.plan_month(conn, parsed, ym, with_after=False)
    if request.form.get("fingerprint") != fingerprint(base):
        flash("The workbook or data changed after this preview. Create a new preview before applying.", "warning")
        return redirect(url_for("imports.batch", batch_id=batch_id, month=ym))
    confirm, excluded, distinct = _choices(request.form)
    plan = P.plan_month(conn, parsed, ym, with_after=False, excluded=excluded, confirm=confirm, distinct=distinct)
    selected = set(request.form.getlist("include"))
    reviewed = {i["property_id"] for i in plan["properties"] + [plan["business"]] if i and (i["status"] == "review" or i.get("flagged"))} & selected
    if reviewed and request.form.get("ack") != "1":
        flash("Some selected properties have flagged checks. Tick \"I have reviewed the flagged items\" to import them, or untick them.", "error")
        return redirect(url_for("imports.batch", batch_id=batch_id, month=ym))
    if not selected:
        flash("Nothing selected to import.", "error")
        return redirect(url_for("imports.batch", batch_id=batch_id, month=ym))
    try:
        result = A.apply_batch(conn, batch_id, plan, selected, "owner")
    except A.ImportRefused as exc:
        flash(str(exc), "error")
        return redirect(url_for("imports.batch", batch_id=batch_id, month=ym))
    made = f" {len(result['created'])} new propert{'y' if len(result['created']) == 1 else 'ies'} created." if result["created"] else ""
    flash(f"Imported {month_label(ym)}: {len(result['touched'])} area(s) updated, {result['rows']} row change(s).{made} You can undo this from the import page.", "success")
    return redirect(url_for("imports.batch", batch_id=batch_id))


@bp.route("/imports/<int:batch_id>/cancel", methods=["POST"])
def cancel(batch_id):
    B.cancel(db.get_conn(), batch_id)
    flash("Import cancelled. Nothing was changed.", "info")
    return redirect(url_for("imports.index"))


@bp.route("/imports/<int:batch_id>/undo", methods=["POST"])
def undo(batch_id):
    conn = db.get_conn()
    try:
        result = A.undo_batch(conn, batch_id, "owner")
    except A.ImportRefused as exc:
        flash(str(exc), "error")
        return redirect(url_for("imports.batch", batch_id=batch_id))
    flash("Import undone. " + ("The dashboard figures are back exactly as they were before it." if result["restored_exactly"]
                               else "Rows were restored; some figures differ from before because other data changed since."), "success")
    return redirect(url_for("imports.batch", batch_id=batch_id))
