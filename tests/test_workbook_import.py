"""Golden tests for the monthly workbook import (scratch database, copy of the workbook).

  A  clean September import           F  malformed month label
  B  same workbook twice -> 0 dupes   G  refund / negative values accepted
  C  one corrected value re-imported  H  Opex + Capex reconciliation
  D  a new, unmapped property sheet   I  undo restores the exact previous state
  E  a missing property sheet         J  historical months are untouched
  +  the credential sheet is never opened; nothing but parsed data is stored

The real dashboard.db and the user's workbook are never modified: the workbook is
copied to a temp dir first, and the database is a throw-away seeded with the same
properties and fee models.

Run: .venv/bin/python tests/test_workbook_import.py
Set UN_TEST_WORKBOOK to point at a different copy of Biz_Accounts_Tracker_2026_Sept_v4.xlsx.
"""
import hashlib
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="un-workbook-test-"))
os.environ["DASHBOARD_DB_PATH"] = str(TMP / "t.db")
os.environ["DASHBOARD_UPLOADS_PATH"] = str(TMP / "uploads")
os.environ.pop("VERCEL", None)
os.environ.pop("UN_DEMO_MODE", None)
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "dashboard"))
sys.path.insert(0, str(ROOT / "tests"))

SOURCE = Path(os.environ.get("UN_TEST_WORKBOOK", "/Users/Dado/Desktop/Biz_Accounts_Tracker_2026_Sept_v4.xlsx"))
if not SOURCE.exists():
    print(f"SKIP: workbook fixture not found at {SOURCE}")
    sys.exit(0)
COPY = TMP / "workbook-copy.xlsx"
shutil.copy(SOURCE, COPY)
ORIGINAL = COPY.read_bytes()

import db  # noqa: E402
import wbtools  # noqa: E402
from services.workbook import apply as A  # noqa: E402
from services.workbook import batches as B  # noqa: E402
from services.workbook import config as C  # noqa: E402
from services.workbook import identity  # noqa: E402
from services.workbook import plan as P  # noqa: E402
from services.workbook import reader  # noqa: E402

FAILS = []
COUNT = 0


def check(name, cond, detail=""):
    global COUNT
    COUNT += 1
    if not cond:
        FAILS.append(name)
        print(f"  FAIL  {name} {detail}")


db.ensure_schema()
conn = db.get_conn()
SEED = [("11-perryfield-way", "11PW", "11 Perryfield Way", 10.0), ("170-miles-building", "170E", "170 Miles Building", 10.0),
        ("175-miles-building", "175E", "175 Miles Building", 10.0), ("19-draycott-ave", "DRAY", "19 Draycott Ave", 12.0),
        ("44-spooner-road", "S10", "44 Spooner Road", None), ("campbell-hill-w8", "W8", "7A Campden Hill", 15.0),
        ("crested-court", "CC", "40 Crested Court", 15.0), ("lascar-wharf", "LW", "602 Lascar Wharf", None),
        ("nw4", "NW4", "Flat 3 NW4", 15.0), ("tottenham-court-road", "TCR", "Tottenham Court Road", 15.0)]
for pid, code, name, fee in SEED:
    conn.execute("INSERT INTO properties (id, code, name, address, type, management_fee_pct, is_managed) VALUES (?,?,?,?, 'flat', ?, ?)", (pid, code, name, name, fee, 1 if fee else 0))
conn.execute("INSERT INTO properties (id, code, name, address, type) VALUES ('general-overheads','GEN','Portfolio General Expenses','-','overhead')")
identity.seed(conn)


def history(pid, ym, rows, nights):
    """Earlier Excel history for a month: item rows and the aggregate-nights row."""
    for direction, desc, amount, cat in rows:
        conn.execute("INSERT INTO transactions (property_id,date,description,amount,direction,category,source) VALUES (?,?,?,?,?,?,'excel_import')",
                     (pid, f"{ym}-01", desc, amount, direction, cat))
    if nights:
        conn.execute("INSERT INTO bookings (property_id,reservation_id,check_in,check_out,net_revenue,status,source) VALUES (?, 'monthly-aggregate', ?, date(?, ?), 0, 'confirmed','excel_import')",
                     (pid, f"{ym}-01", f"{ym}-01", f"+{nights} days"))


# history that must survive an import of September, and a few September rows the workbook will confirm / change
history("crested-court", "2026-08", [("income", "13-19 saarah", 850, "booking_income"), ("expense", "Cleaning", 408, "cleaning"), ("expense", "Water", 44, "utilities")], 25)
history("lascar-wharf", "2026-08", [("income", "Aug lump", 5904.23, "booking_income"), ("expense", "LW rent", 3377.82, "rent")], 20)
history("crested-court", "2026-09", [("income", "29-06", 656.25, "booking_income"), ("income", "6-8", 236.06, "booking_income"), ("expense", "Cleaning", 300, "cleaning")], 0)
history("19-draycott-ave", "2026-09", [("income", "stay", 1360.54, "booking_income"), ("expense", "FG Mngmt Fee (12%)", 241.94, "management_fee")], 0)
history("general-overheads", "2026-09", [("expense", "General", 3030.0, "other"), ("expense", "G-suite", 5.9, "software")], 0)
history("general-overheads", "2026-08", [("expense", "Claude", 18.0, "software")], 0)
# rows from OTHER sources in the same property-month: an import must never touch them
conn.execute("INSERT INTO documents (id, filename, stored_path, doc_type, status) VALUES (1,'stmt.csv','/x','booking_statement','confirmed')")
conn.execute("INSERT INTO transactions (id, property_id,date,vendor,description,amount,direction,category,source,document_id) VALUES (900001,'crested-court','2026-09-12','Amazon','uploaded invoice',33.5,'expense','purchase','upload',1)")
conn.execute("INSERT INTO transactions (id, property_id,date,description,amount,direction,category,source) VALUES (900002,'crested-court','2026-09-20','hand entered',12,'expense','other','manual')")
conn.execute("INSERT INTO bookings (id, property_id,platform,reservation_id,check_in,check_out,gross_revenue,net_revenue,status,source,document_id) VALUES (900003,'crested-court','airbnb','HMABC123','2026-09-03','2026-09-06',300,250,'confirmed','upload',1)")
conn.commit()
OTHER_SOURCES = "SELECT id, amount, description FROM transactions WHERE id IN (900001,900002) UNION ALL SELECT id, net_revenue, reservation_id FROM bookings WHERE id=900003"

SEP = "2026-09"


def stage(data, name="Biz_Accounts_Tracker_2026_Sept_v4.xlsx"):
    c = db.get_conn()
    bid = B.stage(c, data, name)
    row, parsed = B.load(c, bid)
    return c, bid, parsed


def snapshot(c, where="1=1"):
    parts = []
    for table, order in (("transactions", "id"), ("bookings", "id"), ("booking_source_state", "property_id, month")):
        parts.append(json.dumps([tuple(r) for r in c.execute(f"SELECT * FROM {table} ORDER BY {order}")], default=str))
    return hashlib.sha256("|".join(parts).encode()).hexdigest()


def rows_outside_sept(c):
    return [tuple(r) for r in c.execute("SELECT * FROM transactions WHERE substr(date,1,7)!=? ORDER BY id", (SEP,))] + \
           [tuple(r) for r in c.execute("SELECT * FROM bookings WHERE substr(check_in,1,7)!=? ORDER BY id", (SEP,))]


def item(plan, code):
    return next(i for i in plan["properties"] if i["code"] == code)


def new_batch(c):
    return c.execute("INSERT INTO import_batches (filename, file_hash, status) VALUES ('t.xlsx','x','staged')").lastrowid


# ------------------------------------------------------------------ safety
print("security: the credential sheet")
opened = []
real_load = reader.openpyxl.load_workbook


class Spy:
    def __init__(self, wb):
        self._wb = wb

    def __getitem__(self, name):
        opened.append(name)
        return self._wb[name]

    def close(self):
        self._wb.close()


reader.openpyxl.load_workbook = lambda *a, **k: Spy(real_load(*a, **k))
parsed0 = reader.parse_workbook(ORIGINAL, "w.xlsx")
reader.openpyxl.load_workbook = real_load
check("credential sheet excluded by name", parsed0["roles"]["Logins & Providers"][0] == "blocked")
check("credential sheet never opened", "Logins & Providers" not in opened and len(opened) > 0, opened)
check("only allowlisted sheets opened", set(opened) <= {n for n, (r, _d) in parsed0["roles"].items() if r in ("main", "breakdown", "property")})
check("archive / comparison / prior-year sheets ignored", all(parsed0["roles"][n][0] == "ignored" for n in ("2025 vs 2026", "Copy of Main Page26", "Main25", "CC25", "MCR26")))
check("year detected from the sheet names", parsed0["year"] == 2026)
c, bid0, parsed = stage(ORIGINAL)
stored = c.execute("SELECT parsed, validation FROM import_batches WHERE id=?", (bid0,)).fetchone()
check("only parsed data stored, no file", not any((TMP / "uploads").glob("*")) if (TMP / "uploads").exists() else True)
check("workbook hash recorded", c.execute("SELECT file_hash FROM import_batches WHERE id=?", (bid0,)).fetchone()[0] == hashlib.sha256(ORIGINAL).hexdigest())

# ------------------------------------------------------------------ A
print("A  clean September import")
plan = P.plan_month(c, parsed, SEP)
for code in ("CC", "LW", "W8", "170E", "175E", "TCR", "11PW"):
    check(f"A {code} importable", item(plan, code)["status"] == "ok", item(plan, code)["status"])
check("A 22 Perryfield Way is mapped but not in the dashboard -> proposed as a NEW PROPERTY, nothing created", item(plan, "22PW")["status"] == "new_property"
      and not db.get_conn().execute("SELECT 1 FROM properties WHERE id='22-perryfield-way'").fetchone())
check("A Manchester (MCR26) is never offered as a property", parsed0["roles"]["MCR26"][0] == "ignored" and all(i["code"] != "MCR" for i in plan["properties"]))
check("A 19 Draycott is flagged for review (blank control totals)", item(plan, "19Draycott")["status"] == "review")
check("A 44 Spooner Road has no activity", item(plan, "S10")["status"] == "no_activity")
cc = item(plan, "CC")
check("A CC: 2 existing income rows recognised as unchanged", cc["counts"]["UNCHANGED"] >= 2, cc["counts"])
check("A CC: existing 'Cleaning' 300 reported as CHANGED to 378", any(d["status"] == "CHANGED" and d["old"]["amount"] == 300 and abs(d["new"]["amount"] - 378) < 1e-9 for d in cc["rows"]))
check("A business: lump 'General' 3030 reported REMOVED (itemised rows replace it)", any(d["status"] == "REMOVED" and d["old"]["description"] == "General" for d in plan["business"]["rows"]))
before_all = snapshot(c)
outside_before = rows_outside_sept(c)
ids = [i["property_id"] for i in plan["properties"] if i["status"] in ("ok", "review") and i["code"] != "19Draycott"] + ["general-overheads"]   # NW4 is ticked by hand
c.execute("UPDATE import_batches SET status='staged' WHERE id=?", (bid0,))
res = A.apply_batch(c, bid0, plan, set(ids), "test")
check("A applied: batch marked applied with before/after/row count", c.execute("SELECT status, row_count FROM import_batches WHERE id=?", (bid0,)).fetchone()["status"] == "applied" and res["rows"] > 50)
check("A batch rows are traceable to the batch", c.execute("SELECT COUNT(*) FROM transactions WHERE import_batch_id=?", (bid0,)).fetchone()[0] > 100)


def view(pid):
    return P.dashboard_view(c, pid, SEP)


v = view("crested-court")
check("A CC revenue = workbook income 3684.98", abs(v["revenue"] - 3684.98) < 0.01, v)
check("A CC management fee earned = workbook fee row 552.75", abs(v["management_fee"] - 552.75) < 0.01, v)
check("A CC property costs = 697.86 workbook (676.59 opex + 21.27 capex) + 45.50 uploaded/hand-entered, which stay", abs(v["property_costs"] - 743.36) < 0.01, v)
check("A CC days booked 29, occupancy 29/30", v["days"] == 29 and abs(v["occupancy"] - 29 / 30) < 1e-3, v)
lw = view("lascar-wharf")
check("A LW (operated) revenue 4465.80, costs 3473.21, Property Profit 992.60", abs(lw["revenue"] - 4465.80) < 0.01 and abs(lw["property_costs"] - 3473.21) < 0.01 and abs(lw["profit"] - 992.60) < 0.01, lw)
check("A LW days 23", lw["days"] == 23, lw)
tcr = view("tottenham-court-road")
check("A TCR: its fee row is labelled 'Management Faris 15%' (no word 'fee') -> still a management_fee row, ONE of them, and Property Costs are the 290 of cleaning only", [r_["amount"] for r_ in c.execute("SELECT amount FROM transactions WHERE property_id='tottenham-court-road' AND category='management_fee' AND date LIKE '2026-09%'")] == [1795.707] and abs(view("tottenham-court-road")["property_costs"] - 290.0) < 0.01, view("tottenham-court-road"))
check("A TCR (managed, no fee row on sheet) fee = Main Page 1795.71", abs(tcr["management_fee"] - 1795.71) < 0.01, tcr)
biz = view("general-overheads")
check("A business costs = items + salaries (15,960.02 total less NW4/LW echo rows)", abs(biz["business_costs"] - (15960.0175 - 6519.46 - 3473.2075)) < 0.01, biz)
check("A echo rows (NW4, Lascar Wharf) NOT imported as business costs", not c.execute("SELECT 1 FROM transactions WHERE property_id='general-overheads' AND date LIKE '2026-09%' AND lower(description) IN ('nw4','lascar wharf')").fetchone())
check("A 'General' subtotal not imported on top of its items", not c.execute("SELECT 1 FROM transactions WHERE property_id='general-overheads' AND date LIKE '2026-09%' AND description='General'").fetchone())
check("A booking source for the month is the workbook (explicit)", c.execute("SELECT active_source FROM booking_source_state WHERE property_id='lascar-wharf' AND month=?", (SEP,)).fetchone()["active_source"] == "legacy_aggregate")
check("A NW4 is flagged: dashboard models it as managed (15%) but the workbook records no fee", item(plan, "NW4")["status"] == "review" and
      any(ch["metric"] == "Management model" and ch["status"] == "REVIEW" for ch in item(plan, "NW4")["checks"]), item(plan, "NW4")["status"])
dv = A.verify_item(c, parsed, "19Draycott", SEP)
check("A Draycott (blank workbook totals): shown as NO CONTROL, not a pass", {r["metric"]: r["status"] for r in dv["rows"]}["Income / booking revenue"] == "NO CONTROL", dv["rows"])
for code in ("CC", "LW", "TCR", "W8", "170E", "175E", "11PW"):
    vr = A.verify_item(c, parsed, code, SEP)
    check(f"A verify {code}: workbook = ledger = dashboard", all(r["status"] == "PASS" for r in vr["rows"]), [r for r in vr["rows"] if r["status"] != "PASS"])
check("A NW4: negative guest refund (-135) imported, not rejected", c.execute("SELECT 1 FROM transactions WHERE property_id='nw4' AND date LIKE '2026-09%' AND direction='income' AND amount<0").fetchone() is not None)
check("A Main Page income / 'Other' blocks never touch the ledger", not c.execute("SELECT 1 FROM transactions WHERE source='workbook' AND description LIKE '%Referral%'").fetchone())

# ------------------------------------------------------------------ J
print("J  historical months untouched")
check("J every row outside September is unchanged", rows_outside_sept(c) == outside_before)
check("J uploaded / hand-entered rows in the same property-month are never touched", len(c.execute(OTHER_SOURCES).fetchall()) == 3)

# ------------------------------------------------------------------ B
print("B  same workbook twice")
after_first = snapshot(c)
c2, bid_b, parsed_b = stage(ORIGINAL)
plan_b = P.plan_month(c2, parsed_b, SEP)
changes = sum(i.get("change_count", 0) for i in plan_b["properties"] if i["status"] in ("ok", "review", "unchanged"))
check("B second plan finds zero changes for the importable properties", all(i.get("change_count", 0) == 0 for i in plan_b["properties"] if i["status"] in ("ok", "unchanged")),
      [(i["code"], i.get("change_count")) for i in plan_b["properties"]])
check("B second plan: business costs have nothing to change", plan_b["business"].get("change_count") == 0, plan_b["business"].get("change_count"))
try:
    A.apply_batch(c2, bid_b, plan_b, set(ids), "test")
    check("B re-import refused (nothing to import)", False)
except A.ImportRefused:
    check("B re-import refused (nothing to import)", True)
check("B ledger identical after the second attempt (zero duplicates)", snapshot(db.get_conn()) == after_first)
dupes = c.execute("""SELECT COUNT(*) FROM (SELECT property_id,date,description,amount,direction,source_ref FROM transactions WHERE source='workbook'
                     GROUP BY 1,2,3,4,5,6 HAVING COUNT(*)>1)""").fetchone()[0]
check("B no duplicated workbook cell in the ledger", dupes == 0, dupes)

# ------------------------------------------------------------------ C
print("C  one corrected value, re-imported")
cc_parsed = parsed["properties"]["CC"]
old = next(i["amount"] for i in cc_parsed["opex"][SEP]["items"] if i["label"] == "Cleaning")
tot = cc_parsed["opex"][SEP]["total"]
S = cc_parsed["summary"][SEP]
new_val = 400.0
d = new_val - old
cc_fix = wbtools.patch(ORIGINAL, edits={
    ("CC26", "AC10"): new_val, ("CC26", "AC20"): tot + d, ("CC26", "G15"): S["opex"] + d,
    ("CC26", "F15"): S["total_costs"] - d, ("CC26", "C15"): S["net"] - d, ("CC26", "D15"): S["operating"] - d})
c3, bid_c, parsed_c = stage(cc_fix)
plan_c = P.plan_month(c3, parsed_c, SEP)
ccc = item(plan_c, "CC")
check("C only CC differs among the imported properties", [i["code"] for i in plan_c["properties"] if i.get("change_count") and i["code"] not in ("19Draycott", "22PW")] == ["CC"], [(i["code"], i.get("change_count")) for i in plan_c["properties"]])
check("C exactly one CHANGED row: Cleaning 378 -> 400", ccc["counts"]["CHANGED"] == 1 and ccc["counts"]["NEW"] == 0 and ccc["counts"]["REMOVED"] == 0 and
      any(d["status"] == "CHANGED" and d["old"]["amount"] == 378 and d["new"]["amount"] == 400 for d in ccc["rows"]), ccc["counts"])
check("C corrected workbook still reconciles", ccc["status"] == "ok", ccc["status"])
n_before = c3.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
snap_pre_c = snapshot(c3)
A.apply_batch(c3, bid_c, plan_c, {"crested-court"}, "test")
check("C row count unchanged (replaced, not added)", c3.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == n_before)
check("C Cleaning now 400", c3.execute("SELECT amount FROM transactions WHERE property_id='crested-court' AND date LIKE '2026-09%' AND description='Cleaning'").fetchone()[0] == 400)
check("C batch #2 touched only CC", json.loads(c3.execute("SELECT properties FROM import_batches WHERE id=?", (bid_c,)).fetchone()[0]) == ["crested-court"])
check("C property costs moved by exactly the correction (22)", abs(P.dashboard_view(c3, "crested-court", SEP)["property_costs"] - 743.36 - 22) < 0.01)
res_i = A.undo_batch(c3, bid_c, "test")
check("C/I undo of the correction restores the value 378 and the exact ledger", res_i["restored_exactly"] and snapshot(c3) == snap_pre_c)
check("C/I first batch is still applied after undoing the second", c3.execute("SELECT status FROM import_batches WHERE id=?", (bid0,)).fetchone()[0] == "applied")

# ------------------------------------------------------------------ D / E
print("D  new property sheet   E  missing property sheet")
c4, bid_d, parsed_d = stage(wbtools.patch(ORIGINAL, renames={"S1026": "ZZ26"}))
plan_d = P.plan_month(c4, parsed_d, SEP)
zz = item(plan_d, "ZZ")
check("D a sheet whose title matches an existing property's alias is THAT property, not a new one", zz["property_id"] == "44-spooner-road" and zz["status"] != "new_property", zz["status"])
check("D ...so no duplicate property exists", c4.execute("SELECT COUNT(*) FROM properties WHERE name LIKE '%Spooner%'").fetchone()[0] == 1)
c4b, bid_d2, parsed_d2 = stage(wbtools.patch(ORIGINAL, edits={("LW26", "B2"): "9 Test Street"}, renames={"LW26": "NEW26"}))
plan_d2 = P.plan_month(c4b, parsed_d2, SEP)
nw = item(plan_d2, "NEW")
check("D a genuinely new sheet is proposed as a NEW PROPERTY with its sheet, alias and an unknown model", nw["status"] == "new_property" and nw["new_property"]["sheet"] == "NEW26"
      and "NEW" in nw["new_property"]["aliases"] and nw["new_property"]["model"] is None, nw["new_property"])
check("D validation report lists it", B.validation_report(parsed_d2)["unmapped"][0]["sheet"] == "NEW26")
check("D nothing exists until applied", not c4b.execute("SELECT 1 FROM properties WHERE name='9 Test Street'").fetchone())
try:
    A.apply_batch(c4b, bid_d2, plan_d2, {nw["property_id"]}, "test")
    check("D creation refused until the model is confirmed", False)
except A.ImportRefused:
    check("D creation refused until the model is confirmed", True)
c5, bid_e, parsed_e = stage(wbtools.patch(ORIGINAL, renames={"TCR26": "TCRX"}))
plan_e = P.plan_month(c5, parsed_e, SEP)
check("E missing sheet is reported and that property is left alone", item(plan_e, "TCR")["status"] == "missing_sheet" and "left exactly as it is" in item(plan_e, "TCR")["reasons"][0])
tcr_before = c5.execute("SELECT COUNT(*), ROUND(SUM(amount),2) FROM transactions WHERE property_id='tottenham-court-road'").fetchone()
try:
    A.apply_batch(c5, bid_e, plan_e, {"tottenham-court-road"}, "test")
    check("E cannot import the missing property", False)
except A.ImportRefused:
    check("E cannot import the missing property", True)
check("E existing TCR data untouched", tuple(c5.execute("SELECT COUNT(*), ROUND(SUM(amount),2) FROM transactions WHERE property_id='tottenham-court-road'").fetchone()) == tuple(tcr_before))

# ------------------------------------------------------------------ F
print("F  malformed month label")
c6, bid_f, parsed_f = stage(wbtools.patch(ORIGINAL, edits={("CC26", "AB9"): "Septembr"}))
plan_f = P.plan_month(c6, parsed_f, SEP)
check("F malformed month is an error naming the cell", any(i["code"] == "malformed_month" and "AB9" in i["message"] for i in parsed_f["issues"]))
check("F that property is blocked, the others are not", item(plan_f, "CC")["status"] == "error" and item(plan_f, "LW")["status"] in ("ok", "unchanged"), item(plan_f, "CC")["status"])
try:
    A.apply_batch(c6, bid_f, plan_f, {"crested-court"}, "test")
    check("F blocked property cannot be imported", False)
except A.ImportRefused:
    check("F blocked property cannot be imported", True)
c6b, _b, parsed_f2 = stage(wbtools.patch(ORIGINAL, edits={("CC26", "AC10"): "four hundred"}))
check("F non-numeric amount is an error", any(i["code"] == "non_numeric" for i in parsed_f2["issues"]) and item(P.plan_month(c6b, parsed_f2, SEP), "CC")["status"] == "error")
c6c, _b2, parsed_f3 = stage(wbtools.patch(ORIGINAL, edits={("CC26", "I15"): 1.4}))
check("F occupancy over 100% is blocked", item(P.plan_month(c6c, parsed_f3, SEP), "CC")["status"] == "error")
c6d, _b3, parsed_f4 = stage(wbtools.patch(ORIGINAL, edits={("CC26", "J15"): -3}))
check("F negative booked nights is blocked", item(P.plan_month(c6d, parsed_f4, SEP), "CC")["status"] == "error")

# ------------------------------------------------------------------ G
print("G  refund / negative expense")
heat = next(i["amount"] for i in cc_parsed["opex"][SEP]["items"] if i["label"] == "Heating")
d = -12.0 - heat
refund = wbtools.patch(ORIGINAL, edits={
    ("CC26", "AB17"): "Refund: heating", ("CC26", "AC17"): -12.0, ("CC26", "AC20"): tot + d, ("CC26", "G15"): S["opex"] + d,
    ("CC26", "F15"): S["total_costs"] - d, ("CC26", "C15"): S["net"] - d, ("CC26", "D15"): S["operating"] - d})
c7, bid_g, parsed_g = stage(refund)
plan_g = P.plan_month(c7, parsed_g, SEP)
g = item(plan_g, "CC")
check("G negative expense is accepted and reconciles", g["status"] == "ok" and all(ch["status"] == "PASS" for ch in g["checks"]), [ch for ch in g["checks"] if ch["status"] != "PASS"])
check("G the refund row is a negative expense", any(d_["new"] and d_["new"]["amount"] == -12.0 and d_["new"]["direction"] == "expense" for d_ in g["rows"]))
A.apply_batch(c7, bid_g, plan_g, {"crested-court"}, "test")
check("G refund reduces property costs", abs(P.dashboard_view(c7, "crested-court", SEP)["property_costs"] - (743.36 - heat - 12)) < 0.01)

# ------------------------------------------------------------------ H
print("H  Opex + Capex reconciliation")
nw = item(plan, "NW4")
checks = {ch["metric"]: ch for ch in nw["checks"]}
check("H NW4 Opex 2519.46 / Capex 4000 / Total 6519.46 all PASS", all(checks[m]["status"] == "PASS" for m in ("Opex", "Capex", "Total costs", "Income", "Days booked", "Occupancy")), {m: checks[m]["status"] for m in checks})
check("H NW4 capex rows flagged capex=1 (Sourcing Fee, Furniture)", sorted(r["description"] for r in db.get_conn().execute("SELECT description FROM transactions WHERE property_id='nw4' AND date LIKE '2026-09%' AND capex=1")) == ["Furniture", "Sourcing Fee"])
check("H CC capex 21.27 comes from the breakdown's own formula range", abs(sum(r[0] for r in db.get_conn().execute("SELECT amount FROM transactions WHERE property_id='crested-court' AND date LIKE '2026-09%' AND capex=1")) - 21.27) < 0.001)
check("H purchases lump replaced by the itemised breakdown (no lump row)", not db.get_conn().execute("SELECT 1 FROM transactions WHERE property_id='crested-court' AND date LIKE '2026-09%' AND lower(description)='purchases' AND source='workbook'").fetchone())
dr = {ch["metric"]: ch for ch in item(plan, "19Draycott")["checks"]}
check("H Draycott: its fee row sits outside the block's own total -> reported as not counted (never silent)", dr["Rows the workbook's own total does not count"]["status"] == "REVIEW" and "FG Mngmt Fee" in dr["Rows the workbook's own total does not count"]["note"])

# ------------------------------------------------------------------ I
print("I  undo")
c8 = db.get_conn()
snap_pre = snapshot(c8)
plan_i = P.plan_month(c8, parsed_c, SEP)          # the "cleaning 400" workbook vs the ledger as it stands (refund version applied)
bid_i = new_batch(c8)
A.apply_batch(c8, bid_i, plan_i, {"crested-court"}, "test")
snap_mid = snapshot(c8)
check("I import changed the ledger", snap_mid != snap_pre)
# editing a row the import wrote makes an undo unsafe
row = c8.execute("SELECT id FROM transactions WHERE import_batch_id=? LIMIT 1", (bid_i,)).fetchone()
c8.execute("UPDATE transactions SET amount=amount+1 WHERE id=?", (row[0],))
c8.commit()
try:
    A.undo_batch(c8, bid_i, "test")
    check("I undo refused when an imported row was edited afterwards", False)
except A.ImportRefused:
    check("I undo refused when an imported row was edited afterwards", True)
c8.execute("UPDATE transactions SET amount=amount-1 WHERE id=?", (row[0],))
c8.commit()
r = A.undo_batch(c8, bid_i, "test")
check("I undo restores exactly the previous dashboard state", r["restored_exactly"] and snapshot(c8) == snap_pre)
try:
    A.undo_batch(c8, bid_i, "test")
    check("I a batch cannot be undone twice", False)
except A.ImportRefused:
    check("I a batch cannot be undone twice", True)
check("I batch marked undone", c8.execute("SELECT status FROM import_batches WHERE id=?", (bid_i,)).fetchone()[0] == "undone")
check("I uploaded / hand-entered rows are still there after the undo", len(c8.execute(OTHER_SOURCES).fetchall()) == 3)
bad = A.ImportRefused
try:
    A.undo_batch(c8, bid0, "test")
    undone_first = False                # batch g changed CC afterwards, so batch 1 cannot be undone yet
except bad:
    undone_first = True
check("I an earlier import cannot be undone while a later one overlaps it", undone_first)
A.undo_batch(c8, bid_g, "test")
A.undo_batch(c8, bid0, "test")
undone_first = True
check("I undoing the first full import works and leaves the pre-import rows back", undone_first and snapshot(c8) == before_all, "")

# ------------------------------------------------------------------ L
print("L  workbook quirks found in the real file")
plan_jan = P.plan_month(c8, parsed, "2026-01", with_after=False)
e170 = item(plan_jan, "170E")
check("L 170E Jan: the unlabelled 8.14 its block total counts is imported (as '(no label)') and its income reconciles",
      any(d["new"] and d["new"]["description"] == "(no label)" and abs(d["new"]["amount"] - 8.14) < 1e-9 for d in e170["rows"])
      and {c["metric"]: c["status"] for c in e170["checks"]}["Income"] == "PASS", e170["status"])
check("L 170E Jan: the stale Main Page fee (146.26 vs the sheet's 147.08) is flagged",
      any(c["metric"] == "Management fee" and c["status"] == "REVIEW" for c in e170["checks"]))
check("L unlabelled amounts are reported, not silent", any(i["code"] == "unlabelled_amount" for i in parsed["issues"]))
tcr_rows = [d["new"] for d in item(plan, "TCR")["rows"] if d["new"] and d["new"]["category"] == "management_fee"]
check("L TCR (no fee row on its sheet): the Main Page fee 1795.71 is recorded as a management_fee row", len(tcr_rows) == 1 and abs(tcr_rows[0]["amount"] - 1795.707) < 1e-6, tcr_rows)
check("L ...and does not disturb TCR's Opex reconciliation", all(ch["status"] == "PASS" for ch in item(plan, "TCR")["checks"]), [ch for ch in item(plan, "TCR")["checks"] if ch["status"] != "PASS"])
plan_apr = P.plan_month(c8, parsed, "2026-04", with_after=False)
rate = [ch for ch in item(plan_apr, "TCR")["checks"] if ch["metric"] == "Management fee rate"]
check("L TCR April: a fee that is not 15% of the income is flagged", rate and rate[0]["status"] == "REVIEW", rate)
cc_jan = [ch for ch in item(plan_jan, "CC")["checks"] if ch["metric"] == "Management fee"]
check("L CC January: stale Main Page fee (346.18 vs sheet 393.46) is flagged", cc_jan and cc_jan[0]["status"] == "REVIEW", cc_jan)
check("L 11PW June: workbook occupancy that disagrees with its Days Booked is flagged",
      any(ch["metric"] == "Occupancy" and ch["status"] == "REVIEW" for ch in item(P.plan_month(c8, parsed, "2026-06", with_after=False), "11PW")["checks"]))
w8_jan = item(plan_jan, "W8")
check("L W8 January: a text '£1.59' amount in the breakdown blocks that property, with the cell named", w8_jan["status"] == "error" and any("D76" in r for r in w8_jan["reasons"]), w8_jan["reasons"])
nw4_jul = item(P.plan_month(c8, parsed, "2026-07", with_after=False), "NW4")
check("L NW4 July: workbook blank but dashboard has rows -> would remove them, so flagged", nw4_jul["status"] in ("review", "no_activity") and (nw4_jul["status"] != "review" or any("blank" in r for r in nw4_jul["reasons"])))

# ------------------------------------------------------------------ K  the pages
print("K  pages and gates")
import io  # noqa: E402
import re  # noqa: E402
from app import create_app  # noqa: E402

client = create_app().test_client()
snap_k0 = snapshot(db.get_conn())
check("K import page and reframed Documents page load", client.get("/imports").status_code == 200 and
      b"Source documents" in client.get("/documents").data and b"Import monthly workbook" in client.get("/documents").data)
bad = client.post("/imports/upload", data={"workbook": (io.BytesIO(b"not a workbook"), "notes.txt")}, content_type="multipart/form-data")
check("K non-Excel upload refused", bad.status_code == 302 and c8.execute("SELECT COUNT(*) FROM import_batches WHERE filename='notes.txt'").fetchone()[0] == 0)
junk = client.post("/imports/upload", data={"workbook": (io.BytesIO(b"PK junk"), "broken.xlsx")}, content_type="multipart/form-data", follow_redirects=True)
check("K corrupt .xlsx gives a friendly message, not a 500", junk.status_code == 200 and b"not an .xlsx workbook" in junk.data or b"could not be opened" in junk.data)
up = client.post("/imports/upload", data={"workbook": (io.BytesIO(ORIGINAL), "Biz_Accounts_Tracker_2026_Sept_v4.xlsx")}, content_type="multipart/form-data")
loc = up.headers["Location"]
page = client.get(loc).data.decode()
check("K preview defaults to September (latest month where most properties have income)", "Import September 2026 only" in page)
check("K preview shows NEW PROPERTY DETECTED (22 Perryfield Way) and the credential exclusion", "excluded by name" in page and "NEW PROPERTY DETECTED" in page and "22 Perryfield Way" in page)
check("K preview shows now -> workbook figures and reconciliation", "Reconciliation (workbook vs what would be imported)" in page and "PASS" in page)
fp = re.search(r'name="fingerprint" value="([^"]+)"', page).group(1)
ticked = re.findall(r'name="include" value="([^"]+)" checked', page)
check("K only clean properties are pre-ticked (NW4 / Draycott need a deliberate tick)", "nw4" not in ticked and "19-draycott-ave" not in ticked and "crested-court" in ticked, ticked)
n0 = c8.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
r = client.post(loc + "/apply", data={"month": SEP, "fingerprint": "stale", "include": ticked})
check("K a stale preview is refused and nothing is written", c8.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == n0)
r = client.post(loc + "/apply", data={"month": SEP, "fingerprint": fp, "include": ticked + ["nw4"]})
check("K a flagged property needs the acknowledgement tick", c8.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == n0)
r = client.post(loc + "/apply", data={"month": SEP, "fingerprint": fp, "include": ticked})
bid_k = int(loc.rsplit("/", 1)[1])
check("K apply through the page records an applied batch", c8.execute("SELECT status FROM import_batches WHERE id=?", (bid_k,)).fetchone()[0] == "applied")
led = client.get(f"/expenses?from={SEP}-01&to={SEP}-01&t_batch={bid_k}").data.decode()
check("K the ledger can be filtered to one import's rows, and shows the batch link", f"Import #{bid_k}" in led and f"#{bid_k}</a>" in led)
check("K provenance line appears for the imported month and not for an untouched one",
      b"Updated from workbook" in client.get(f"/expenses?from={SEP}-01&to={SEP}-01").data and b"Updated from workbook" not in client.get("/expenses?from=2026-05-01&to=2026-05-01").data)
done = client.get(loc).data.decode()
check("K applied page shows before/after, reconciliation and workbook = ledger = dashboard", "Before and after" in done and "Workbook = ledger = dashboard" in done and "Undo import" in done)
snap_applied = snapshot(db.get_conn())
client.post(loc + "/undo")
check("K undo through the page restores the pre-import ledger", c8.execute("SELECT status FROM import_batches WHERE id=?", (bid_k,)).fetchone()[0] == "undone" and snapshot(db.get_conn()) == snap_k0)
check("K sort / filter on the batch list works", client.get("/imports?status=applied&sort=period&dir=asc").status_code == 200 and client.get("/imports?sort=bogus&status=bogus").status_code == 200)
import subprocess  # noqa: E402
demo_env = {**os.environ, "UN_DEMO_MODE": "1"}
probe = subprocess.run([sys.executable, "-c",
    "import sys,io;sys.path.insert(0,r'%s');from app import create_app;c=create_app().test_client();"
    "r=c.post('/imports/upload',data={'workbook':(io.BytesIO(b'x'),'a.xlsx')},content_type='multipart/form-data');"
    "p=c.get('/imports').data;print(r.status_code, b'Imports are off in this demo' in p)" % (ROOT / "dashboard")],
    env=demo_env, capture_output=True, text=True)
check("K demo mode: uploads refused and the page says imports are off", probe.stdout.strip() == "302 True", probe.stdout + probe.stderr[-300:])

# ---------------------------------------------------------------- summary
print("provenance")
check("provenance helper finds nothing after undo", B.provenance(c8, "crested-court", SEP) is None)
print(f"\n{COUNT - len(FAILS)}/{COUNT} checks passed" + ("" if not FAILS else f"; FAILED: {FAILS}"))
sys.exit(1 if FAILS else 0)
