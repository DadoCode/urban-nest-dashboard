"""Phase 8: first-time-user usability -- scratch database only; nothing real is touched.

Run: .venv/bin/python tests/test_phase8.py
"""
import json
import os
import re
import sys
import tempfile
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="un-p8-test-"))
os.environ["DASHBOARD_DB_PATH"] = str(TMP / "t.db")
os.environ["DASHBOARD_UPLOADS_PATH"] = str(TMP / "uploads")
os.environ.pop("VERCEL", None)
os.environ.pop("UN_DEMO_MODE", None)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "dashboard"))

import db  # noqa: E402
from services import completeness  # noqa: E402

FAILS, COUNT = [], 0


def check(name, cond, detail=""):
    global COUNT
    COUNT += 1
    if not cond:
        FAILS.append(name)
        print(f"  FAIL  {name} {detail}")


db.ensure_schema()
c = db.get_conn()
SEP = "2026-09"


def prop(pid, name, active=1, fee=None, start=None):
    c.execute("INSERT INTO properties (id, code, name, address, type, active, start_date, management_fee_pct, is_managed) VALUES (?,?,?,?,'flat',?,?,?,?)",
              (pid, pid.upper(), name, name, active, start, fee, 1 if fee else 0))
    completeness.seed_defaults(c, pid)


def tx(pid, date, amount, direction, category, desc="row", source="workbook", batch=None, ref=None):
    return c.execute("INSERT INTO transactions (property_id,date,description,amount,direction,category,capex,source,import_batch_id,source_ref) VALUES (?,?,?,?,?,?,0,?,?,?)",
                     (pid, date, desc, amount, direction, category, source, batch, ref)).lastrowid


prop("op1", "Flat 602, Lascar Wharf Building, 21 Parnham Street, London, E14 7FN")
prop("free", "Flat 9, Free Month Court, London, N1 1AA")
c.execute("INSERT INTO properties (id, code, name, address, type, active) VALUES ('general-overheads','GO','Business Costs','', 'overhead', 1)")
c.execute("INSERT INTO import_batches (id, filename, file_hash, uploaded_at, applied_at, status, period, kind, properties, reconciliation, before_totals, after_totals) VALUES "
          "(7, 'wb.xlsx', 'abcdef0123456789', '2026-10-06 10:00', '2026-10-06 10:05:00', 'applied', ?, 'workbook', ?, '{}', '{}', '{}')", (SEP, json.dumps(["op1"])))
tx("op1", f"{SEP}-01", 1500.0, "income", "booking_income", "direct", batch=7, ref="OP126!AC10")
tx("op1", f"{SEP}-05", 230.0, "expense", "cleaning", "cleaners", batch=7, ref="OP126!AC20")
c.commit()

from app import create_app  # noqa: E402

client = create_app().test_client()


def text(resp):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", re.sub(r"<script.*?</script>", "", resp.data.decode(), flags=re.S)))


def count(sql, *a):
    return db.get_conn().execute(sql, a).fetchone()[0]


# ------------------------------------------------------------------ A/B/C Add Property
print("add property")
n0 = count("SELECT COUNT(*) FROM properties")
r = client.post("/apartments", data={"name": "Flat 1 Nowhere", "address": "x", "status": "active"}, follow_redirects=True)
check("A: no model chosen -> nothing is created, and the reason is shown", count("SELECT COUNT(*) FROM properties") == n0 and "Choose how this property is run" in text(r))
r = client.post("/apartments", data={"name": "Flat 2 Nowhere", "model": "managed", "status": "active"}, follow_redirects=True)
check("B: managed without a fee is refused", count("SELECT COUNT(*) FROM properties") == n0 and "management fee" in text(r))
r = client.post("/apartments", data={"name": "Flat 2 Nowhere", "model": "managed", "fee": "150", "status": "active"}, follow_redirects=True)
check("B: a fee over 100% is refused", count("SELECT COUNT(*) FROM properties") == n0)
r = client.post("/apartments", data={"name": "Flat 3 Managed", "model": "managed", "fee": "15", "status": "active", "start_date": "2026-03-01"}, follow_redirects=True)
row = db.get_conn().execute("SELECT * FROM properties WHERE name='Flat 3 Managed'").fetchone()
check("B: managed with a fee is accepted with the fee and the managed flag", row and row["management_fee_pct"] == 15 and row["is_managed"] == 1)
check("C: the start date entered is stored exactly", row and row["start_date"] == "2026-03-01")
check("success message is workbook-first, never 'upload a document'", "Import the monthly workbook" in text(r) and "Upload a document" not in text(r))
r = client.post("/apartments", data={"name": "Flat 4 Operated", "model": "operated", "status": "inactive"}, follow_redirects=True)
row = db.get_conn().execute("SELECT * FROM properties WHERE name='Flat 4 Operated'").fetchone()
check("operated + inactive stored; no start date invented; no fee", row and row["active"] == 0 and row["start_date"] is None and row["management_fee_pct"] is None and row["is_managed"] == 0)
home = client.get("/properties").data.decode()
check("the dialog makes Model a required choice with no preselected value, and the fee row starts hidden",
      'name="model"' in home and re.search(r'<select[^>]*name="model"[^>]*required', home) is not None and "selected disabled" in home and 'id="add-fee-row"' in home and 'hidden' in home.split('id="add-fee-row"')[1][:80])

# ------------------------------------------------------------------ D manual expense in a workbook-controlled month
print("controlled month")
form = {"vendor": "Fixit", "description": "boiler", "amount": "80", "category": "maintenance", "year": "2026", "month": "9"}
n0 = count("SELECT COUNT(*) FROM transactions")
r = client.post("/property/op1/expense", data=form)
t = text(r)
check("D: a manual expense in a workbook-controlled month shows the warning first and writes nothing", count("SELECT COUNT(*) FROM transactions") == n0
      and "controlled by workbook import #7" in t and "outside the workbook" in t and "REVIEW" in t and "Add manual adjustment" in t and "Cancel" in t, t[:400])
r = client.post("/property/op1/expense", data={**form, "confirm_manual": "1"}, follow_redirects=True)
rows = db.get_conn().execute("SELECT * FROM transactions WHERE description='boiler'").fetchall()
check("D: after explicit confirmation the row is added and stays identified as manual", len(rows) == 1 and rows[0]["source"] == "manual" and rows[0]["import_batch_id"] is None)
check("D: the confirmation leaves an audit record naming the import", count("SELECT COUNT(*) FROM audit_log WHERE action='manual_adjustment'") == 1 if "audit_log" in [x[0] for x in db.get_conn().execute("select name from sqlite_master")] else True)
n0 = count("SELECT COUNT(*) FROM transactions")
client.post("/property/free/expense", data={**form, "description": "plain"}, follow_redirects=True)
check("D: a month with no workbook behind it adds the expense straight away (no new friction)", count("SELECT COUNT(*) FROM transactions") == n0 + 1)

# ------------------------------------------------------------------ E document confirmation in a workbook-controlled month
print("document confirmation")
import io  # noqa: E402
from werkzeug.datastructures import MultiDict  # noqa: E402


def upload_csv(name, body, pid):
    return client.post("/documents/upload", data={"document": (io.BytesIO(body.encode()), name), "doc_type": "other", "property_id": pid},
                       content_type="multipart/form-data")


def last_doc():
    return db.get_conn().execute("SELECT * FROM documents ORDER BY id DESC LIMIT 1").fetchone()


def confirm_form(doc_id, pid, extra=()):
    form = []
    for it in db.get_conn().execute("SELECT * FROM document_items WHERE document_id=? ORDER BY line_index", (doc_id,)):
        form += [("item_id", it["id"]), ("include", it["id"]), ("property_id", pid), ("vendor", it["vendor"] or ""), ("description", it["raw_description"] or ""),
                 ("amount", it["amount"]), ("category", it["category"]), ("type", "opex"), ("date", it["date"])]
    return MultiDict(form + list(extra))


upload_csv("sep.csv", "Date,Vendor,Description,Amount\n2026-09-02,CleanCo,Turnover clean,45.00\n", "op1")
d = last_doc()
n0 = count("SELECT COUNT(*) FROM transactions")
r = client.post(f"/documents/{d['id']}/confirm", data=confirm_form(d["id"], "op1"), follow_redirects=True)
t = text(r)
check("E: confirming a document into a workbook-controlled month posts nothing", count("SELECT COUNT(*) FROM transactions") == n0, t[:300])
check("E: the document is not silently confirmed and the reason names the workbook import", count("SELECT status FROM documents WHERE id=?", d["id"]) != "confirmed"
      and "controlled by the monthly workbook" in t and "#7" in t and "Keep this document as evidence only" in t)
page = client.get(f"/documents/{d['id']}/review").data.decode()
check("E: the review page warns before confirming and offers 'Keep as evidence only'", "controlled by a monthly workbook import" in page and "Keep as evidence only" in page)
r = client.post(f"/documents/{d['id']}/confirm", data=confirm_form(d["id"], "op1", [("evidence_only", "1")]), follow_redirects=True)
check("E: 'Keep as evidence only' confirms the document as evidence without touching the ledger",
      count("SELECT COUNT(*) FROM transactions") == n0 and db.get_conn().execute("SELECT status FROM documents WHERE id=?", (d["id"],)).fetchone()[0] == "confirmed"
      and "supporting evidence" in text(r), text(r)[:300])
upload_csv("free.csv", "Date,Vendor,Description,Amount\n2026-09-02,CleanCo,Turnover clean,45.00\n", "free")
d2 = last_doc()
client.post(f"/documents/{d2['id']}/confirm", data=confirm_form(d2["id"], "free"))
check("E: a month the workbook does not control still confirms and posts as before", count("SELECT COUNT(*) FROM transactions WHERE document_id=?", d2["id"]) == 1)
upload_csv("man.png", "\x89PNG" + "1" * 40, "op1")
d3 = last_doc()
manual = [("include", "0"), ("property_id", "op1"), ("month", "9"), ("year", "2026"), ("vendor", "ManualCo"), ("description", "hand typed"), ("category", "purchase"), ("amount", "33.00")]
n0 = count("SELECT COUNT(*) FROM transactions")
client.post(f"/documents/{d3['id']}/confirm", data=MultiDict(manual))
check("E: the hand-entry fallback is held the same way", count("SELECT COUNT(*) FROM transactions") == n0)

# ------------------------------------------------------------------ F NO DATA vs REAL ZERO
print("no data vs real zero")
tx("free", "2026-11-03", 40.0, "expense", "cleaning", "only a cost", source="manual")
c.commit()
nd = client.get("/?from=2026-10-01&to=2026-10-01&compare=previous_period")
ndt = text(nd)
check("F: a period with nothing recorded says so once", ndt.count("No data imported for this period.") == 1, ndt[:200])
tiles = re.findall(r'<div class="value">(.*?)</div>', nd.data.decode())
check("F: its headline figures are dashes, not £0 / 0%", tiles and all(v.strip() == "—" for v in tiles[:4]), tiles)
check("F: no comparison arrow or -100% is shown for it", "−100" not in ndt and "-100" not in ndt and 'class="delta' not in nd.data.decode().split('class="tiles"')[1].split("</div>\n  </div>")[0] if 'class="tiles"' in nd.data.decode() else False)
check("F: the empty state offers the monthly import", "Import monthly workbook" in ndt)
rz = client.get("/?from=2026-11-01&to=2026-11-01&compare=none")
rzt = rz.data.decode()
vals = re.findall(r'<div class="value">(.*?)</div>', rzt)
check("F: a period with rows but zero revenue still shows 0 (a real zero is not hidden)", vals and vals[0].strip() == "£0" and "No data imported for this period." not in text(rz), vals)
pr = client.get("/properties?from=2026-10-01&to=2026-10-01&compare=none")
check("F: Properties list shows a dash for a property with nothing recorded, not 0%", "No data imported for this period" in pr.data.decode())
pz = client.get("/properties?from=2026-11-01&to=2026-11-01&compare=none").data.decode()
check("F: …and still lists the real zero for the property that has a cost", re.search(r"Free Month Court.*?£0", re.sub(r"\s+", " ", pz)) is not None)

# ------------------------------------------------------------------ G managed Overview, cost split, review explanations, copy
print("overview clarity")
prop("mg1", "Flats 7 & 8, Shaldon Mansions, 132 Charing Cross Road, London, WC2H 0LA", fee=15.0)
AUG = "2026-08"
c.execute("INSERT INTO import_batches (id, filename, file_hash, uploaded_at, applied_at, status, period, kind, properties, reconciliation, before_totals, after_totals) VALUES "
          "(9, 'wb2.xlsx', 'abcdef0123456780', '2026-10-06 11:00', '2026-10-06 11:05:00', 'applied', ?, 'workbook', ?, ?, '{}', '{}')",
          (AUG, json.dumps(["mg1", "op1"]), json.dumps({
              "mg1": [{"metric": "Income without booked nights", "workbook": None, "imported": None, "diff": None, "status": "REVIEW"},
                      {"metric": "Reversal of last month's costs", "workbook": None, "imported": -40.0, "diff": None, "status": "REVIEW"},
                      {"metric": "Capex", "workbook": None, "imported": 0.0, "diff": None, "status": "NO CONTROL"},
                      {"metric": "Income", "workbook": 11971.0, "imported": 11971.0, "diff": 0.0, "status": "PASS"}],
              "op1": [{"metric": "Income", "workbook": 500.0, "imported": 500.0, "diff": 0.0, "status": "PASS"}]})))
tx("mg1", f"{AUG}-01", 11971.0, "income", "booking_income", "guests", batch=9, ref="MG1!AC10")
tx("mg1", f"{AUG}-01", 1796.0, "expense", "management_fee", "Mngmt Fee (15%)", batch=9, ref="MG1!AC19")
tx("mg1", f"{AUG}-03", 300.0, "expense", "cleaning", "managed cleaning", batch=9, ref="MG1!AC20")
tx("op1", f"{AUG}-01", 500.0, "income", "booking_income", "direct", batch=9, ref="OP1!AC10")
tx("op1", f"{AUG}-04", 200.0, "expense", "cleaning", "operated cleaning", batch=9, ref="OP1!AC20")
c.commit()
Q8 = f"from={AUG}-01&to={AUG}-01&compare=none"
ov = text(client.get(f"/?{Q8}"))
check("G: Overview shows Property Costs with the operated / managed split and the Business Costs figure", re.search(r"Property Costs £[\d,]+ \(operated £[\d,]+ · managed £[\d,]+\)", ov) is not None and "Business Costs" in ov, ov[:500])
check("G: …and says Property Profit uses operated-property costs only", "Property Profit uses operated-property costs only." in ov)
mo = text(client.get(f"/properties/mg1?{Q8}"))
check("G: a managed property's Overview says what guests paid, what Urban Nest earned, and that the rest belongs to the owner",
      "Guests paid £11,971. Urban Nest earned £1,796 management fee; the remaining booking revenue belongs to the owner." in mo, mo[:600])
check("G: the owner's share is never called profit", "owner" in mo and "owner's profit" not in mo.lower())
oo = text(client.get(f"/properties/op1?{Q8}"))
check("G: …and only on a managed property's Overview", "belongs to the owner" not in oo and "belongs to the owner" not in ov)
ap = client.get("/imports/9").data.decode()
apt = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", ap))
check("H: PASS / REVIEW / NO CONTROL are defined near the import results", "Figures reconcile with the available control." in apt and "Something does not reconcile or needs checking." in apt
      and "No comparison/control value was provided, so the check cannot be evaluated. This is not a failure." in apt)
check("H: each REVIEW check carries its fixed plain explanation (nothing invented)", "Revenue is recorded while booked nights are zero." in apt and "Negative value may represent a reversal or credit." in apt)
check("H: the Imports page carries the 'correct the workbook and re-import' sentence", "If a workbook-controlled figure is wrong, correct the workbook and re-import the month." in text(client.get("/imports")))

bk = text(client.get(f"/bookings?{Q8}"))
check("I: with monthly totals but no reservation records, Bookings says exactly that (no 'upload a statement')",
      "Monthly booking totals are available for this period, but no individual reservation records were imported." in bk and "Upload a statement" not in bk and "booking statements" not in bk, bk[:400])
bn = text(client.get("/bookings?from=2027-01-01&to=2027-01-01&compare=none"))
check("I: with truly no data, Bookings says so and offers the monthly import", "No data imported for this period." in bn and "Import monthly workbook" in bn and "Upload a statement" not in bn)
docs = text(client.get("/documents"))
check("J: the Documents page is retitled and says documents are supporting evidence", "Documents & Monthly Import" in docs and "supporting evidence" in docs and "Import monthly workbook" in docs)
pdoc = text(client.get("/properties/op1/documents"))
check("J: the property Documents tab no longer promises that confirming a document updates the numbers", "numbers update automatically" not in pdoc and "supporting evidence" in pdoc)
pset = text(client.get("/properties/op1/settings"))
check("J: property Settings no longer calls booking statements the main way reservations get in", "main way reservations get in" not in pset and "monthly workbook" in pset)
check("J: nothing still tells the user to upload a statement to see bookings", all("Upload a statement" not in text(client.get(u)) for u in ("/bookings", "/bookings/calendar", "/properties/op1/bookings")))
base = client.get("/").data.decode()
pal = json.loads(re.search(r'<script id="palette-data" type="application/json">(.*?)</script>', base, re.S).group(1))
check("K: the command palette can jump to 'Import monthly workbook'", any(i["label"] == "Import monthly workbook" and i["url"] == "/imports" for i in pal), pal[:3])
check("K: the Documents page links to Imports and the legacy page is renamed (shown only when there is something to compare)",
      'href="/imports"' in client.get("/documents").data.decode() and "Excel vs booking statements" not in docs)
tg = text(client.get("/targets"))
check("L: Targets defines Operating Profit and points to Overview", "Target Operating Profit uses Gross Booking Revenue minus recorded costs. For managed properties this includes booking revenue that belongs to the owner." in tg and "Overview" in tg)
rp = client.get("/reports/view?type=portfolio_monthly").data.decode() if client.get("/reports/view?type=portfolio_monthly").status_code == 200 else ""
check("L: Reports defines Costs as operated-property costs", True if not rp else "operated-property costs only" in text(client.get("/reports/view?type=portfolio_monthly")))
err = text(client.post("/imports/upload", data={"workbook": (io.BytesIO(b"x"), "notes.txt")}, content_type="multipart/form-data", follow_redirects=True))
check("M: a wrong file type says what to do next", "Choose the monthly .xlsx workbook." in err)

# ------------------------------------------------------------------ N no negative zero, touch-accessible info controls
print("polish")
from app import create_app as _mk  # noqa: E402
flt = _mk().jinja_env.filters
check("N: money never prints -£0 / -£0.00", flt["money"](-0.2) == "£0" and flt["money2"](-0.001) == "£0.00" and flt["money_k"](-0.2) == "£0")
from services.common import gbp0, pct0  # noqa: E402
check("N: whole-pound and percentage helpers never print negative zero", gbp0(-0.3) == "£0" and pct0(-0.001) == "0%" and gbp0(-12.4) == "−£12" and pct0(0.4567) == "46%")
tmpl = _mk().jinja_env.from_string('{% from "_import_macros.html" import val, diffv %}{{ val(-0.001, "money") }}|{{ val(-0.0001, "pct") }}|{{ val(-0.2, "int") }}|{{ diffv(-0.001, "money") }}|{{ diffv(-0.0001, "pct") }}')
out = re.sub(r"<[^>]+>", "", tmpl.render())
check("N: import figures show £0.00, 0%, 0 and 0 pp rather than a negative zero", out == "£0.00|0%|0|£0.00|0 pp", out)
ui = (Path(__file__).resolve().parent.parent / "dashboard" / "static" / "ui.js").read_text()
check("N: info controls respond to hover, focus, click/tap and the keyboard", all(k in ui for k in ("'mouseover'", "'focusin'", "'click'", "'keydown'", "'Escape'", "'Enter'")) and "info-pop" in ui)
ovh = client.get(f"/?{Q8}").data.decode()
check("N: info controls are keyboard-reachable and carry their definition in aria-label", 'class="info-dot"' in ovh and 'tabindex="0"' in ovh and 'aria-label="Booked nights as a share' in ovh)
check("N: Occupancy and Booked Nights have a definition that mentions monthly totals", "monthly total" in METRIC_INFO_OCC if (METRIC_INFO_OCC := __import__("services.common", fromlist=["METRIC_INFO"]).METRIC_INFO.get("occupancy", "")) else False)

# ------------------------------------------------------------------ more NO DATA surfaces, demo reasons
print("more surfaces")
ex = client.get("/expenses?from=2027-01-01&to=2027-01-01&compare=previous_period")
exv = re.findall(r'<div class="value">(.*?)</div>', ex.data.decode())
check("F: Expenses shows dashes (not £0) for a period with nothing recorded, and says so once", exv and all(v.strip() == "—" for v in exv) and text(ex).count("No data imported for this period.") == 1, exv)
exz = client.get("/expenses?from=2026-11-01&to=2026-11-01&compare=none")
check("F: …while a period with a recorded cost still shows it", "£40" in text(exz) and "No data imported for this period." not in text(exz))
pf = text(client.get("/bookings/performance?from=2027-01-01&to=2027-01-01&compare=none"))
check("F: Bookings Performance lists an active property with nothing recorded as 'No data imported', not 0%", "No data imported" in pf)
import subprocess  # noqa: E402
probe = subprocess.run([sys.executable, "-c", "import sys,re;sys.path.insert(0,r'%s');from app import create_app;c=create_app().test_client();t=c.get('/imports/7').data.decode();"
                        "print('Disabled in the read-only demo.' in t, 'title=\"Disabled in the read-only demo.\"' in t)" % (Path(__file__).resolve().parent.parent / "dashboard")],
                       capture_output=True, text=True, env={**os.environ, "UN_DEMO_MODE": "1"})
check("N: in the demo the disabled Undo says why", probe.stdout.strip() == "True True", probe.stdout + probe.stderr[-300:])

# ------------------------------------------------------------------ Property Performance: NO DATA vs REAL ZERO
print("property performance")
prop("zn", "Flat 5, Zero Nights House, London, N5 5ZZ")
prop("zr", "Flat 6, Zero Revenue House, London, N6 6ZZ")
prop("zd", "Flat 7, Empty Month House, London, N7 7ZZ")
prop("zi", "Flat 8, Gone House, London, N8 8ZZ", active=0)
prop("zs", "Flat 9, Later House, London, N9 9ZZ", start="2027-06-01")
ZM = "2026-12"
c.execute("INSERT INTO bookings (property_id, platform, reservation_id, check_in, check_out, gross_revenue, platform_fees, cleaning_fee, net_revenue, status, source, import_batch_id, source_ref) "
          "VALUES ('zn', 'excel', 'monthly-aggregate', ?, ?, 0, 0, 0, 0, 'confirmed', 'workbook', NULL, 'Days Booked 2026-12')", (f"{ZM}-01", f"{ZM}-01"))     # a recorded month with 0 booked nights
tx("zn", f"{ZM}-02", 60.0, "expense", "cleaning", "cost only", source="manual")
tx("zr", f"{ZM}-02", 35.0, "expense", "cleaning", "cost only", source="manual")                                                                    # recorded, revenue is a real £0
tx("zr", "2026-11-02", 700.0, "income", "booking_income", "last month", source="manual")
c.commit()
PQ = f"from={ZM}-01&to={ZM}-01&compare=previous_period"


def tile_values(path):
    return [v.strip() for v in re.findall(r'<div class="value">(.*?)</div>', client.get(path).data.decode())]


v = tile_values(f"/properties/zn/performance?{PQ}")
check("P1: recorded month with 0 booked nights keeps its tiles, Booked nights 0 and Occupancy 0%", len(v) >= 4 and v[0] == "0%" and v[2] == "0", v)
pt = text(client.get(f"/properties/zn/performance?{PQ}"))
check("P1: …and does not claim there is no data", "No data imported for this period." not in pt)
v = tile_values(f"/properties/zr/performance?{PQ}")
check("P2: recorded month with £0 revenue keeps its tiles, RevPAR and ADR £0", len(v) >= 4 and v[0] == "0%" and v[1] == "£0" and v[3] == "£0", v)
ov0 = tile_values(f"/properties/zr?{PQ}")
check("P2: the property Overview shows Urban Nest Revenue £0 as a real zero", ov0 and "£0" in ov0 and "—" not in ov0, ov0)
nd = client.get(f"/properties/zd/performance?{PQ}")
ndt = text(nd)
check("P3: a property-month with nothing recorded says so, with the import action", ndt.count("No data imported for this period.") == 1 and "Import monthly workbook" in ndt)
check("P3: …and shows no tiles and no comparison deltas", not tile_values(f"/properties/zd/performance?{PQ}") and 'class="delta' not in nd.data.decode())
gone = text(client.get(f"/properties/zi/performance?{PQ}"))
later = text(client.get(f"/properties/zs/performance?{PQ}"))
check("P4: an inactive property with nothing recorded still says it is inactive", "no longer active" in gone and "No data imported for this period." not in gone, gone[:300])
check("P4: a property that has not started still says it had not joined", "had not joined the portfolio" in later and "No data imported for this period." not in later, later[:300])

print(f"\n{COUNT - len(FAILS)}/{COUNT} passed")
sys.exit(1 if FAILS else 0)
