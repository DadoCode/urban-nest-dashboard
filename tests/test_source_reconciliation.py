"""Golden tests for explicit per-property-month booking-source selection.

The rule under test: uploading or confirming reservations NEVER changes which
data feeds the KPIs. The Excel history stays in charge of a month until a person
switches it (after seeing the projected impact); the switch is reversible and
the Excel rows are never modified.

Known data (scratch database; the real ledger is never touched):

  prop-a   Aug 2026  Excel lump GBP 5,000, aggregate 25 nights
           Sep 2026  Excel lump GBP 3,000, aggregate 20 nights
  prop-b   Aug 2026  Excel lump GBP 2,000, aggregate 10 nights
  prop-a   Oct 2026  no Excel history at all

Run: .venv/bin/python tests/test_source_reconciliation.py
"""
import hashlib
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="un-source-test-"))
os.environ["DASHBOARD_DB_PATH"] = str(TMP / "t.db")
os.environ["DASHBOARD_UPLOADS_PATH"] = str(TMP / "uploads")
os.environ.pop("VERCEL", None)
os.environ.pop("UN_DEMO_MODE", None)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "dashboard"))

import db  # noqa: E402
import services.kpis as kpis  # noqa: E402
import services.reconcile as rc  # noqa: E402
import services.sources as src  # noqa: E402

db.ensure_schema()
c = db.get_conn()
for pid, fee in (("prop-a", None), ("prop-b", 15.0)):
    c.execute("INSERT INTO properties (id, code, name, address, type, management_fee_pct) VALUES (?,?,?,?, 'flat', ?)", (pid, pid.upper(), pid.title(), pid, fee))
for doc in (1, 2, 3):
    c.execute("INSERT INTO documents (id, filename, stored_path, doc_type, status) VALUES (?,?,?,?, 'confirmed')", (doc, f"doc{doc}.csv", f"/x/{doc}", "booking_statement"))


def legacy(pid, ym, lump, nights):
    c.execute("INSERT INTO transactions (property_id,date,vendor,description,amount,direction,category,source) VALUES (?,?,?,?,?,'income','booking_income','excel_import')",
              (pid, f"{ym}-01", "Excel", "monthly income", lump))
    end = f"{ym}-{nights + 1:02d}"
    c.execute("INSERT INTO bookings (property_id,platform,reservation_id,check_in,check_out,gross_revenue,net_revenue,status,source) VALUES (?,NULL,'monthly-aggregate',?,?,0,0,'confirmed','excel_import')",
              (pid, f"{ym}-01", end))


legacy("prop-a", "2026-08", 5000, 25)
legacy("prop-a", "2026-09", 3000, 20)
legacy("prop-b", "2026-08", 2000, 10)
c.execute("INSERT INTO transactions (property_id,date,vendor,description,amount,direction,category,source) VALUES ('prop-a','2026-08-15','Excel','cost',400,'expense','cleaning','excel_import')")
c.commit()


def reservation(pid, code, ci, co, net, doc, source="upload", platform="airbnb"):
    cc = db.get_conn()
    cc.execute("INSERT INTO bookings (property_id,platform,reservation_id,check_in,check_out,gross_revenue,net_revenue,status,source,document_id) VALUES (?,?,?,?,?,?,?,'confirmed',?,?)",
               (pid, platform, code, ci, co, net * 1.2, net, source, doc))
    cc.commit(); cc.close()


def legacy_fingerprint():
    cc = db.get_conn()
    rows = cc.execute("SELECT * FROM transactions WHERE source='excel_import' ORDER BY id").fetchall() + cc.execute("SELECT * FROM bookings WHERE source='excel_import' ORDER BY id").fetchall()
    cc.close()
    return hashlib.sha256(repr([tuple(r) for r in rows]).encode()).hexdigest()


AUG, SEP, OCT = kpis.month_bounds(2026, 8), kpis.month_bounds(2026, 9), kpis.month_bounds(2026, 10)
FUNCS = ("revenue", "accommodation_revenue", "booked_nights", "reservation_count", "occupancy", "adr", "revpar", "costs")


def snapshot(pid, bounds_list=(AUG, SEP, OCT, ("2026-08-10", "2026-09-20"))):
    cc = db.get_conn()
    out = {(pid, b, f): round(getattr(kpis, f)(cc, pid, *b), 6) for b in bounds_list for f in FUNCS}
    out.update({(pid, b, "adj_" + k): round(v, 6) for b in bounds_list for k, v in kpis.adjusted_kpi_snapshot(cc, pid, *b).items()})
    cc.close()
    return out


failures = []


def check(label, ok, extra=""):
    print(("[PASS] " if ok else "[FAIL] ") + label + ("" if ok else f"   {extra}"))
    if not ok:
        failures.append(label)


def kp(f, pid, bounds):
    cc = db.get_conn(); v = getattr(kpis, f)(cc, pid, *bounds); cc.close(); return v


finger0 = legacy_fingerprint()
base_a, base_b, base_all = snapshot("prop-a"), snapshot("prop-b"), snapshot(None)
check("baseline: August = Excel (GBP 5,000 / 25 nights)", kp("revenue", "prop-a", AUG) == 5000 and kp("booked_nights", "prop-a", AUG) == 25)

# ---------------------------------------------------------------- A. one reservation does not replace the month
reservation("prop-a", "R1", "2026-08-10", "2026-08-12", 300, 1)
check("A. one uploaded reservation (GBP 300 / 2 nights) does NOT replace August's Excel figures",
      kp("revenue", "prop-a", AUG) == 5000 and kp("booked_nights", "prop-a", AUG) == 25 and kp("accommodation_revenue", "prop-a", AUG) == 5000,
      (kp("revenue", "prop-a", AUG), kp("booked_nights", "prop-a", AUG)))

# ---------------------------------------------------------------- B. a partial statement changes no KPI
check("B. a partial statement changes no KPI -- every figure for the property, the other property and the portfolio is identical",
      snapshot("prop-a") == base_a and snapshot("prop-b") == base_b and snapshot(None) == base_all)

# ---------------------------------------------------------------- C. several documents contribute to the detailed total
reservation("prop-a", "R2", "2026-08-14", "2026-08-18", 700, 2, platform="booking_com")      # a second document (another platform)
cc = db.get_conn()
existing, uploaded = rc.metrics(cc, "prop-a", "2026-08", src.LEGACY), rc.metrics(cc, "prop-a", "2026-08", src.DETAILED)
detail = rc.uploaded_detail(cc, "prop-a", "2026-08")
cc.close()
check("C. two documents (Airbnb + Booking.com) are united in the detailed figures: GBP 1,000 / 6 nights / 2 reservations",
      uploaded["gbr"] == 1000 and uploaded["nights"] == 6 and uploaded["reservations"] == 2 and {d["id"] for d in detail["documents"]} == {1, 2}, (uploaded, detail["documents"]))
check("C. the existing (Excel) side of the comparison is still GBP 5,000 / 25 nights, and the dashboard still shows it",
      existing["gbr"] == 5000 and existing["nights"] == 25 and kp("revenue", "prop-a", AUG) == 5000)
check("C. the comparison hints that the upload is INCOMPLETE rather than deciding anything", rc.verdict(existing, uploaded)[0].startswith("INCOMPLETE"))

# ---------------------------------------------------------------- confirming/importing never writes a source decision
check("confirming reservations never creates a source decision (no automatic switch)", db.get_conn().execute("SELECT COUNT(*) FROM booking_source_state").fetchone()[0] == 0)
cc = db.get_conn(); st = rc.status_of(cc, "prop-a", "2026-08"); cc.close()
check("status for the month is RECONCILIATION NEEDED, active source still the Excel history", st["label"] == "RECONCILIATION NEEDED" and st["active"] == src.LEGACY and not st["explicit"])

# ---------------------------------------------------------------- D. explicit switch changes numbers exactly once
from app import create_app  # noqa: E402

client = create_app().test_client()
r = client.post("/reconciliation/prop-a/2026-08/use-detailed", data={})
check("D. switching without ticking the confirmation box changes nothing", kp("revenue", "prop-a", AUG) == 5000)
r = client.post("/reconciliation/prop-a/2026-08/use-detailed", data={"confirm": "yes"})
check("D. explicit switch: August now = the detailed bookings (GBP 1,000 / 6 nights / 2 reservations)",
      kp("revenue", "prop-a", AUG) == 1000 and kp("booked_nights", "prop-a", AUG) == 6 and kp("reservation_count", "prop-a", AUG) == 2,
      (kp("revenue", "prop-a", AUG), kp("booked_nights", "prop-a", AUG)))
client.post("/reconciliation/prop-a/2026-08/use-detailed", data={"confirm": "yes"})
check("D. switching again changes nothing further (exactly once)", kp("revenue", "prop-a", AUG) == 1000 and kp("booked_nights", "prop-a", AUG) == 6)
check("D. only the chosen property-month changed: prop-a September, prop-b August, portfolio-other months are as before",
      all(snapshot("prop-a", (SEP,))[k] == v for k, v in base_a.items() if k[1] == SEP) and snapshot("prop-b") == base_b)

# ---------------------------------------------------------------- E. reverting restores the original figures exactly
client.post("/reconciliation/prop-a/2026-08/revert", data={})
check("E. reverting restores every original figure exactly", snapshot("prop-a") == base_a and snapshot(None) == base_all)
cc = db.get_conn()
steps = [(t["old_value"], t["new_value"]) for t in rc.trail(cc, "prop-a", "2026-08")]
cc.close()
check("E. the audit trail reads exactly: Excel -> detailed (selected) -> Excel (reverted); the repeated click left no extra entry",
      steps == [(src.LEGACY, src.DETAILED), (src.DETAILED, src.LEGACY)], steps)

# ---------------------------------------------------------------- F. duplicates are not double counted
reservation("prop-a", "R1", "2026-08-10", "2026-08-12", 300, 3)           # the same reservation uploaded again in another document
reservation("prop-a", None, "2026-08-20", "2026-08-22", 150, 3, source="manual")
reservation("prop-a", None, "2026-08-20", "2026-08-22", 150, 3, source="manual")   # identical manual twin (same dates and amount, no code)
cc = db.get_conn(); up = rc.metrics(cc, "prop-a", "2026-08", src.DETAILED); cc.close()
check("F. the same reservation code from two documents, and identical manual twins, each count once: R1 300 + R2 700 + manual 150 = GBP 1,150 / 8 nights / 3 reservations",
      up["gbr"] == 1150 and up["nights"] == 8 and up["reservations"] == 3, up)

# ---------------------------------------------------------------- G. multi-property statements reconcile per property
reservation("prop-b", "B1", "2026-08-02", "2026-08-05", 450, 1)           # same document 1 as prop-a's R1
client.post("/reconciliation/prop-a/2026-08/use-detailed", data={"confirm": "yes"})
check("G. switching prop-a leaves prop-b (same document) on its Excel figures (GBP 2,000 / 10 nights)",
      kp("revenue", "prop-b", AUG) == 2000 and kp("booked_nights", "prop-b", AUG) == 10 and kp("revenue", "prop-a", AUG) == 1150)
cc = db.get_conn(); rows = {(r["property_id"], r["ym"]): r["status"]["label"] for r in rc.candidates(cc)}; cc.close()
check("G. each property-month has its own status", rows[("prop-a", "2026-08")] == "Using detailed bookings" and rows[("prop-b", "2026-08")] == "RECONCILIATION NEEDED", rows)
client.post("/reconciliation/prop-a/2026-08/revert", data={})

# ---------------------------------------------------------------- H. cross-month stay: 28 Aug -> 3 Sep (6 nights, GBP 600)
reservation("prop-a", "X1", "2026-08-28", "2026-09-03", 600, 2)
client.post("/reconciliation/prop-a/2026-08/use-detailed", data={"confirm": "yes"})      # August detailed, September stays on Excel
cc = db.get_conn()
aug_rev, aug_n = kpis.revenue(cc, "prop-a", *AUG), kpis.booked_nights(cc, "prop-a", *AUG)
sep_rev, sep_n = kpis.revenue(cc, "prop-a", *SEP), kpis.booked_nights(cc, "prop-a", *SEP)
span_rev, span_n = kpis.revenue(cc, "prop-a", "2026-08-01", "2026-10-01"), kpis.booked_nights(cc, "prop-a", "2026-08-01", "2026-10-01")
cc.close()
check("H. August detailed / September Excel: August takes the stay's 4 August nights (GBP 400) on top of the other detailed bookings",
      abs(aug_rev - (1150 + 400)) < 0.01 and aug_n == 8 + 4, (aug_rev, aug_n))
check("H. ...and September stays exactly Excel (GBP 3,000 / 20 nights): the stay's 2 September nights are NOT mixed in",
      sep_rev == 3000 and sep_n == 20, (sep_rev, sep_n))
check("H. a range spanning both months adds each month's own source, once", abs(span_rev - (1550 + 3000)) < 0.01 and span_n == 12 + 20, (span_rev, span_n))
client.post("/reconciliation/prop-a/2026-09/use-detailed", data={"confirm": "yes"})
cc = db.get_conn()
sep_rev2, sep_n2 = kpis.revenue(cc, "prop-a", *SEP), kpis.booked_nights(cc, "prop-a", *SEP)
aug_rev2 = kpis.revenue(cc, "prop-a", *AUG)
cc.close()
check("H. September switched too: it now holds only the stay's 2 September nights (GBP 200); August is unchanged", abs(sep_rev2 - 200) < 0.01 and sep_n2 == 2 and abs(aug_rev2 - aug_rev) < 0.01, (sep_rev2, sep_n2, aug_rev2))
client.post("/reconciliation/prop-a/2026-08/revert", data={})
cc = db.get_conn()
check("H. August back on Excel while September stays detailed: each month keeps its own source",
      kpis.revenue(cc, "prop-a", *AUG) == 5000 and abs(kpis.revenue(cc, "prop-a", *SEP) - 200) < 0.01 and kpis.booked_nights(cc, "prop-a", *AUG) == 25)
cc.close()
client.post("/reconciliation/prop-a/2026-09/revert", data={})

# ---------------------------------------------------------------- mid-month ranges follow the same ownership
check("a range that starts mid-month sees that month's Excel history (no silent drop)", kp("booked_nights", "prop-a", ("2026-08-10", "2026-08-20")) == 10)

# ---------------------------------------------------------------- months with no Excel history use detailed bookings automatically
reservation("prop-a", "O1", "2026-10-05", "2026-10-09", 480, 1)
check("a month with no Excel history (October) uses the uploaded reservation automatically: GBP 480 / 4 nights",
      kp("revenue", "prop-a", OCT) == 480 and kp("booked_nights", "prop-a", OCT) == 4)

# ---------------------------------------------------------------- what-if never persists; every decision is audited
cc = db.get_conn()
with src.forced({("prop-a", "2026-08"): src.DETAILED}):
    inside = kpis.revenue(cc, "prop-a", *AUG)
outside = kpis.revenue(cc, "prop-a", *AUG)
cc.close()
check("the what-if used for projected impact does not leak out of its context", inside != 5000 and outside == 5000)
check("every explicit decision is in the audit log", db.get_conn().execute("SELECT COUNT(*) FROM audit_log WHERE entity_type='booking_source'").fetchone()[0] >= 8)

# ---------------------------------------------------------------- the pages
page = client.get("/reconciliation").get_data(as_text=True)
check("the reconciliation page lists the property-months and the hint", "Prop-A" in page and "Reconciliation" in page and "RECONCILIATION NEEDED" in page)
month_page = client.get("/reconciliation/prop-a/2026-08").get_data(as_text=True)
check("the month page shows Existing / Uploaded / Difference and the projected impact", all(t in month_page for t in ("Existing (Excel monthly aggregate)", "Uploaded so far", "Difference", "Current dashboard", "After the switch", "Use detailed bookings for Aug 2026")))
audit_page = client.get("/audit/figures?from=2026-08&to=2026-10").get_data(as_text=True)
check("the figure audit shows the active source and the source counts for each property-month", "Active source" in audit_page and "Excel aggregate nights" in audit_page and "Uploaded reservations" in audit_page)
csv_text = client.get("/audit/figures?from=2026-08&to=2026-10&format=csv").get_data(as_text=True)
check("the figure audit downloads as CSV", csv_text.splitlines()[0].startswith("Property,Model,Month,Active source") and len(csv_text.splitlines()) > 3)

# ---------------------------------------------------------------- I. the Excel rows are never modified
check("I. after every switch, revert and upload, the Excel rows are byte-for-byte what they were", legacy_fingerprint() == finger0)
check("I. no Excel row was deleted or added", db.get_conn().execute("SELECT COUNT(*) FROM transactions WHERE source='excel_import'").fetchone()[0] == 4
      and db.get_conn().execute("SELECT COUNT(*) FROM bookings WHERE source='excel_import'").fetchone()[0] == 3)

print()
if failures:
    print(f"{len(failures)} check(s) FAILED:")
    for f in failures:
        print("  -", f)
    sys.exit(1)
print("All source-reconciliation checks passed.")
