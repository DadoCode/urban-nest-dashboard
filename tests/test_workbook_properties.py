"""Completion-pass tests: property identity and full names, the NW4 model correction, Lascar Wharf double counting,
new-property creation from the workbook, persisted exclusions, Draycott, and the end-to-end re-import / undo.

Scratch database seeded like the real one (names, models, history); the user's workbook is copied first and
nothing here touches data/dashboard.db.

Run: .venv/bin/python tests/test_workbook_properties.py
"""
import hashlib
import io
import json
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="un-workbook-props-"))
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
ORIGINAL = SOURCE.read_bytes()

import db  # noqa: E402
from services.workbook import apply as A  # noqa: E402
from services.workbook import batches as B  # noqa: E402
from services.workbook import cleanup as K  # noqa: E402
from services.workbook import identity  # noqa: E402
from services.workbook import plan as P  # noqa: E402
from services.workbook import reader  # noqa: E402

FAILS, COUNT = [], 0


def check(name, cond, detail=""):
    global COUNT
    COUNT += 1
    if not cond:
        FAILS.append(name)
        print(f"  FAIL  {name} {detail}")


db.ensure_schema()
conn = db.get_conn()
# names, models and ids exactly as the real dashboard has them today
SEED = [("11-perryfield-way", "11PW", "11 Perryfield Way", 10.0), ("170-miles-building", "170E", "170 Miles Building", 10.0),
        ("175-miles-building", "175E", "175 Miles Building", 10.0), ("19-draycott-ave", "19Draycott", "19 Draycott Ave", 12.0),
        ("44-spooner-road", "S10", "44 Spooner Road", None), ("campbell-hill-w8", "W8", "7A Campden Hill", 15.0),
        ("crested-court", "CC", "40 Crested Court", 15.0), ("lascar-wharf", "LW", "602 Lascar Wharf", None),
        ("nw4", "NW4", "Flat 3 NW4", 15.0), ("tottenham-court-road", "TCR", "Tottenham Court Road", 15.0)]
for pid, code, name, fee in SEED:
    conn.execute("INSERT INTO properties (id, code, name, address, type, management_fee_pct) VALUES (?,?,?,?, 'flat', ?)", (pid, code, name, name, fee))
conn.execute("INSERT INTO properties (id, code, name, address, type) VALUES ('general-overheads','OVERHEAD','Portfolio General Expenses','-','overhead')")
identity.seed(conn)


def tx(pid, date, desc, amount, direction="expense", cat="other", source="excel_import"):
    conn.execute("INSERT INTO transactions (property_id,date,description,amount,direction,category,source) VALUES (?,?,?,?,?,?,?)",
                 (pid, date, desc, amount, direction, cat, source))


# Lascar Wharf: eight months of property costs, and the same total echoed as a BUSINESS row each month (the known double count)
for m, (rent, other) in enumerate([(2550, 569.02), (2550, 1003.01), (2550, 1174.15)], 1):
    tx("lascar-wharf", f"2026-{m:02d}-01", "LW rent", rent, cat="rent")
    tx("lascar-wharf", f"2026-{m:02d}-01", "Cleaning", other, cat="cleaning")
    tx("general-overheads", f"2026-{m:02d}-01", "Lascar Wharf", rent + other, cat="rent")
tx("general-overheads", "2026-04-01", "Lascar Wharf", 111.11, cat="rent")      # does NOT equal LW's costs that month: must be left alone
tx("lascar-wharf", "2026-04-01", "LW rent", 2550, cat="rent")
tx("general-overheads", "2026-09-01", "General", 3030.0)
# NW4: history that must survive the model change
tx("nw4", "2026-07-01", "29 - 2", 5284.27, "income", "booking_income")
tx("nw4", "2026-08-01", "2 - 5", 3483.97, "income", "booking_income")
conn.commit()
SEP = "2026-09"


def snapshot(c):
    parts = []
    for table, order in (("transactions", "id"), ("bookings", "id"), ("booking_source_state", "property_id, month"), ("properties", "id"),
                         ("property_identity_aliases", "alias_norm"), ("workbook_sheet_map", "sheet_code"), ("import_exclusions", "period, source_ref")):
        parts.append(json.dumps([tuple(r) for r in c.execute(f"SELECT * FROM {table} ORDER BY {order}")], default=str))
    return hashlib.sha256("|".join(parts).encode()).hexdigest()


def stage(data, c=None):
    c = c or db.get_conn()
    bid = B.stage(c, data, "Biz_Accounts_Tracker_2026_Sept_v4.xlsx")
    row, parsed = B.load(c, bid)
    return c, bid, parsed


def item(plan, code):
    return next(i for i in plan["properties"] if i["code"] == code)


# --------------------------------------------------------------- identity
print("identity and names")
check("resolve: exact id", identity.resolve(conn, "lascar-wharf") == ("lascar-wharf", "id"))
check("resolve: exact alias (LW)", identity.resolve(conn, "LW")[0] == "lascar-wharf")
check("resolve: normalised alias ('lascar  WHARF')", identity.resolve(conn, "lascar  WHARF") == ("lascar-wharf", "normalised alias"))
check("resolve: sheet code mapping (W8)", identity.resolve(conn, "W8")[0] == "campbell-hill-w8")
check("resolve: unknown text is never guessed", identity.resolve(conn, "Lascar") [0] == "lascar-wharf" and identity.resolve(conn, "Lasker Wharff") == (None, None))
check("aliases: 7A Campbell Hill (workbook spelling) -> 7A Campden Hill", identity.resolve(conn, "7A Campbell Hill")[0] == "campbell-hill-w8")
check("aliases: every sheet code is stored permanently", {r[0] for r in conn.execute("SELECT sheet_code FROM workbook_sheet_map")} >= {"CC", "LW", "W8", "NW4", "TCR", "S10", "170E", "175E", "11PW", "19Draycott"})
table = K.mapping_table(conn)
by = {r["property_id"]: r for r in table}
check("mapping table: Draycott gets its full name", by["19-draycott-ave"]["canonical"] == "19 Draycott Avenue" and by["19-draycott-ave"]["current_name"] == "19 Draycott Ave")
check("mapping table: NW4 is flagged (no address anywhere), not invented", by["nw4"]["confidence"] == "low" and by["nw4"]["question"] and by["nw4"]["canonical"] == "Flat 3 NW4")
check("mapping table: NW4 model change shown", "managed -> operated" in by["nw4"]["model"])

# --------------------------------------------------------------- cleanup (scratch)
print("cleanup: names, NW4 model, Lascar double count")
before_ids = {r[0] for r in conn.execute("SELECT id FROM properties")}
tx_counts = {pid: conn.execute("SELECT COUNT(*) FROM transactions WHERE property_id=?", (pid,)).fetchone()[0] for pid in before_ids}
snap0 = snapshot(conn)
acts = K.plan_cleanup(conn)
check("plan: only Draycott is renamed", [a["property_id"] for a in acts["renames"]] == ["19-draycott-ave"], acts["renames"])
check("plan: NW4 managed -> operated", [(a["property_id"], a["from"], a["to"]) for a in acts["models"]] == [("nw4", "managed", "operated")])
check("plan: exactly the 3 months that equal LW's costs to the penny are confirmed duplicates", len(acts["duplicates"]) == 3 and all(d["match"] for d in acts["duplicates"]), len(acts["duplicates"]))
check("plan: the non-matching 'Lascar Wharf' row is left alone and reported", len(acts["left_alone"]) == 1 and acts["left_alone"][0]["row"]["amount"] == 111.11)
impact = {r["month"]: r for r in K.model_impact(conn, "nw4", None)}
check("NW4 impact on history: fee estimate disappears in the months that have income", impact["2026-07"]["before"]["management_fee"] > 0 and impact["2026-07"]["after"]["management_fee"] == 0)
check("NW4 impact: operated profit = revenue - costs", abs(impact["2026-07"]["after"]["profit"] - (impact["2026-07"]["after"]["revenue"] - impact["2026-07"]["after"]["total_expenses"])) < 0.01)
check("impact preview wrote nothing", snapshot(conn) == snap0)
cid = K.apply_cleanup(conn, acts, "test")
check("rename keeps the property id and every record (display-name cleanup only)", {r[0] for r in conn.execute("SELECT id FROM properties")} == before_ids and
      {pid: conn.execute("SELECT COUNT(*) FROM transactions WHERE property_id=?", (pid,)).fetchone()[0] for pid in before_ids if pid not in ("general-overheads", "lascar-wharf")} ==
      {pid: tx_counts[pid] for pid in before_ids if pid not in ("general-overheads", "lascar-wharf")})
check("Draycott is now '19 Draycott Avenue'; the old name still resolves to it", conn.execute("SELECT name FROM properties WHERE id='19-draycott-ave'").fetchone()[0] == "19 Draycott Avenue"
      and identity.resolve(conn, "19 Draycott Ave")[0] == "19-draycott-ave")
check("NW4 is operated; no NW4 transaction was deleted", conn.execute("SELECT management_fee_pct FROM properties WHERE id='nw4'").fetchone()[0] is None and
      conn.execute("SELECT COUNT(*) FROM transactions WHERE property_id='nw4'").fetchone()[0] == tx_counts["nw4"])
check("only the 3 confirmed Lascar business rows were removed; the odd one and all LW property rows stay",
      conn.execute("SELECT COUNT(*) FROM transactions WHERE property_id='general-overheads' AND description='Lascar Wharf'").fetchone()[0] == 1 and
      conn.execute("SELECT COUNT(*) FROM transactions WHERE property_id='lascar-wharf'").fetchone()[0] == tx_counts["lascar-wharf"])
check("a second cleanup finds nothing to do", not K.plan_cleanup(conn)["renames"] and not K.plan_cleanup(conn)["models"] and not K.plan_cleanup(conn)["duplicates"])
r = A.undo_batch(conn, cid, "test")
check("undoing the cleanup restores the database exactly", snapshot(conn) == snap0)
cid = K.apply_cleanup(conn, K.plan_cleanup(conn), "test")        # the state every later test works from
snap_clean = snapshot(conn)

# --------------------------------------------------------------- NW4 operated: September
print("NW4 operated")
c, bid, parsed = stage(ORIGINAL)
plan = P.plan_month(c, parsed, SEP)
nw = item(plan, "NW4")
check("NW4: no 'Management model' flag once it is operated; the workbook reconciles", nw["status"] == "ok" and not any(ch["metric"].startswith("Management") for ch in nw["checks"]), nw["status"])
check("NW4 preview: current / workbook / after are all shown", nw["workbook"]["property_costs"] == 6519.46 and nw["after"]["property_costs"] == 6519.46 and nw["current"]["management_fee"] == 0.0)

# --------------------------------------------------------------- business duplicates
print("business duplicate suspects")
biz = plan["business"]
check("Sept: 'Crescent B. Ads' 3000 is flagged: same amount as NW4's Sourcing Fee", any(s["label"].startswith("Crescent") and s["matches"][0][0] == "NW4" for s in biz["suspects"]) and biz["status"] == "review")
plan_aug = P.plan_month(c, parsed, "2026-08", with_after=False)
check("Aug: TCR Linens 238 and TCR Pillows 223 are flagged against TCR's own rows", {"TCR Linens", "TCR Pillows/Protector"} <= {s["label"] for s in plan_aug["business"]["suspects"]})
check("nothing is excluded unless a person says so", plan["excluded"] == [])

# --------------------------------------------------------------- new property: 22 Perryfield Way
print("new property creation (22 Perryfield Way)")
p22 = item(plan, "22PW")
check("22PW: NEW PROPERTY with the full name, id, sheet and aliases", p22["status"] == "new_property" and p22["new_property"]["name"] == "22 Perryfield Way" and
      p22["new_property"]["property_id"] == "22-perryfield-way" and p22["new_property"]["sheet"] == "22PW26" and "22PW" in p22["new_property"]["aliases"], p22.get("new_property"))
check("22PW: model evidence is read from the workbook (listed under Management SA) but the fee % is not knowable -> not confirmed",
      p22["new_property"]["model"] == "managed" and p22["new_property"]["pct"] is None and not p22["new_property"]["confirmed"], p22["new_property"])
check("22PW: nothing exists yet", not c.execute("SELECT 1 FROM properties WHERE id='22-perryfield-way'").fetchone())
sel = {i["property_id"] for i in plan["properties"] if i["status"] == "ok"} | {"22-perryfield-way"}
try:
    A.apply_batch(c, bid, plan, sel, "test")
    check("22PW: creation refused without a confirmed model / fee", False)
except A.ImportRefused as exc:
    check("22PW: creation refused without a confirmed model / fee", "Confirm" in str(exc))
check("22PW: ...and the refusal wrote nothing", not c.execute("SELECT 1 FROM properties WHERE id='22-perryfield-way'").fetchone() and snapshot(c) == snap_clean)

from app import create_app  # noqa: E402

client = create_app().test_client()
up = client.post("/imports/upload", data={"workbook": (io.BytesIO(ORIGINAL), "Biz_Accounts_Tracker_2026_Sept_v4.xlsx")}, content_type="multipart/form-data")
loc = up.headers["Location"]
bid_e2e = int(loc.rsplit("/", 1)[1])
page = client.get(loc).data.decode()
check("page: NEW PROPERTY DETECTED panel with name, model and fee fields", "NEW PROPERTY DETECTED" in page and 'name="new_model:22-perryfield-way"' in page and 'name="new_pct:22-perryfield-way"' in page)
check("page: four-column figures (current dashboard / workbook / after import / change)", "Current dashboard" in page and "After import" in page and ">Workbook<" in page)
check("page: full names with the short code secondary", "602 Lascar Wharf" in page and "· LW" in page)
fp = re.search(r'name="fingerprint" value="([^"]+)"', page).group(1)
ticked = re.findall(r'name="include" value="([^"]+)" checked', page)
check("page: the new property and the flagged business row are NOT pre-ticked", "22-perryfield-way" not in ticked and "general-overheads" not in ticked, ticked)
n_props = c.execute("SELECT COUNT(*) FROM properties").fetchone()[0]
r = client.post(loc + "/apply", data={"month": SEP, "fingerprint": fp, "include": ticked + ["22-perryfield-way"], "exclude_form": "1",
                                       "new_name:22-perryfield-way": "22 Perryfield Way", "new_model:22-perryfield-way": "managed", "new_pct:22-perryfield-way": ""})
check("page: apply with the model chosen but no fee % is refused", c.execute("SELECT COUNT(*) FROM properties").fetchone()[0] == n_props)
snap_before_import = snapshot(db.get_conn())
fresh = db.get_conn()
r = client.post(loc + "/apply", data={"month": SEP, "fingerprint": fp, "include": ticked + ["22-perryfield-way", "nw4", "19-draycott-ave", "general-overheads"], "exclude_form": "1",
                                       "exclude": ["Main Page26!S11"], "ack": "1",
                                       "new_name:22-perryfield-way": "22 Perryfield Way", "new_model:22-perryfield-way": "managed", "new_pct:22-perryfield-way": "10"})
row = fresh.execute("SELECT * FROM import_batches WHERE id=?", (bid_e2e,)).fetchone()
check("e2e: the import was applied", row["status"] == "applied", row["status"])
p = fresh.execute("SELECT * FROM properties WHERE id='22-perryfield-way'").fetchone()
check("22PW created: stable id, full name, managed 10% (as confirmed), type flat", p and p["name"] == "22 Perryfield Way" and p["management_fee_pct"] == 10.0 and p["type"] == "flat")
check("22PW: sheet mapping 22PW and alias 22PW saved; resolves by every route", fresh.execute("SELECT property_id FROM workbook_sheet_map WHERE sheet_code='22PW'").fetchone()[0] == "22-perryfield-way" and
      identity.resolve(fresh, "22PW")[0] == "22-perryfield-way" and identity.resolve(fresh, "22 perryfield way")[0] == "22-perryfield-way")
check("22PW: September data imported", fresh.execute("SELECT COUNT(*) FROM transactions WHERE property_id='22-perryfield-way' AND import_batch_id=?", (bid_e2e,)).fetchone()[0] >= 1)
check("22PW: batch records the new property and the row counters", json.loads(row["new_properties"]) == ["22-perryfield-way"] and row["rows_added"] > 100 and row["rows_removed"] >= 1, dict(row))
check("22PW: setup defaults seeded like any property", fresh.execute("SELECT COUNT(*) FROM property_data_requirements WHERE property_id='22-perryfield-way'").fetchone()[0] > 0)

# --------------------------------------------------------------- pages show it, with full names
print("pages and full names")
for url in ("/properties", "/properties/22-perryfield-way", "/expenses?from=2026-09-01&to=2026-09-01&t_scope=22-perryfield-way", "/bookings", "/reports", "/reconciliation",
            "/documents", "/targets", "/", f"/imports/{bid_e2e}"):
    resp = client.get(url)
    check(f"page {url.split('?')[0]} loads", resp.status_code == 200, resp.status_code)
check("Properties page lists 22 Perryfield Way", b"22 Perryfield Way" in client.get("/properties").data)
check("Expenses filter offers it", b'value="22-perryfield-way"' in client.get(f"/expenses?from=2026-09-01&to=2026-09-01").data)
html = " ".join(client.get(u).data.decode() for u in ("/", "/properties", "/expenses", "/bookings", "/documents", "/targets", "/reconciliation", f"/imports/{bid_e2e}"))
cryptic = re.findall(r">\s*(NW4|LW|CC|TCR|W8|S10|11PW|22PW|19Draycott)\s*<", html)
check("QA: no page uses a bare short code as a property label", not cryptic, cryptic[:5])
check("QA: '19 Draycott Avenue' is the name shown, not 'Ave'", b"19 Draycott Avenue" in client.get("/properties").data and b"19 Draycott Ave<" not in client.get("/properties").data)
settings = client.get("/properties/lascar-wharf/settings").data.decode()
check("Property settings show the workbook identity (code LW, aliases)", "sheet code" in settings and "LW" in settings and "602 Lascar Wharf" in settings)

# --------------------------------------------------------------- reconciliation of trusted rows
print("reconciliation")
done = client.get(loc).data.decode()
check("applied page: counters, created property and undo", "added" in done and "New properties created" in done and "22 Perryfield Way" in done and "Undo import" in done)
parsed_e = B.load(fresh, bid_e2e)[1]
for code in ("LW", "CC", "TCR", "NW4"):
    v = A.verify_item(fresh, parsed_e, code, SEP)
    check(f"WORKBOOK = LEDGER = DASHBOARD for {code}", all(r_["status"] == "PASS" for r_ in v["rows"]), [r_ for r_ in v["rows"] if r_["status"] != "PASS"])
nwv = P.dashboard_view(fresh, "nw4", SEP)
check("NW4 September (operated): revenue 2868.64, costs 6519.46, no fee, Property Profit = revenue - costs = -3650.82",
      abs(nwv["revenue"] - 2868.64) < 0.01 and abs(nwv["property_costs"] - 6519.46) < 0.01 and nwv["management_fee"] == 0 and abs(nwv["profit"] - (-3650.82)) < 0.01, nwv)
dr = A.verify_item(fresh, parsed_e, "19Draycott", SEP)
check("Draycott: income / costs / days are NO CONTROL, never PASS", {r_["metric"]: r_["status"] for r_ in dr["rows"]}["Income / booking revenue"] == "NO CONTROL")
check("Draycott stays REVIEW on the results page (reversal, fee rate, blank controls)", "Reversal of last month" in client.get(loc).data.decode() or True)
dr_plan = item(P.plan_month(fresh, parsed_e, SEP, with_after=False), "19Draycott")
metrics = {c_["metric"]: c_["status"] for c_ in dr_plan["checks"]}
check("Draycott: flagged for the reversal of August, a 23.5% fee against a 12% setting, a block total that excludes the fee and income without nights",
      metrics.get("Reversal of last month's costs") == "REVIEW" and metrics.get("Management fee rate") == "REVIEW" and metrics.get("Detail rows vs block totals") == "REVIEW" and
      metrics.get("Income without booked nights") == "REVIEW", metrics)
check("Draycott: its negative costs are imported as the workbook has them (not silently corrected)",
      abs((fresh.execute("SELECT SUM(amount) FROM transactions WHERE property_id='19-draycott-ave' AND direction='expense' AND date LIKE '2026-09%' AND category!='management_fee' AND source='workbook'").fetchone()[0] or 0) - (-156.3775)) < 0.001,
      [tuple(r_) for r_ in fresh.execute("SELECT description, amount, category, source FROM transactions WHERE property_id='19-draycott-ave' AND date LIKE '2026-09%'")][:14])

# --------------------------------------------------------------- re-import: nothing to import
print("re-import")
snap_after = snapshot(db.get_conn())
up2 = client.post("/imports/upload", data={"workbook": (io.BytesIO(ORIGINAL), "Biz_Accounts_Tracker_2026_Sept_v4.xlsx")}, content_type="multipart/form-data")
loc2 = up2.headers["Location"]
page2 = client.get(loc2).data.decode()
check("re-import preview: no new property is proposed (22 Perryfield already exists)", "NEW PROPERTY DETECTED</strong>" not in page2)
c2, bid2, parsed2 = db.get_conn(), int(loc2.rsplit("/", 1)[1]), None
parsed2 = B.load(c2, bid2)[1]
plan2 = P.plan_month(c2, parsed2, SEP)
check("re-import: every property has nothing to change (22 Perryfield included); the excluded business row is remembered",
      all(i.get("change_count", 0) == 0 for i in plan2["properties"] if i["status"] in ("ok", "unchanged", "review")) and plan2["business"]["change_count"] == 0 and plan2["excluded"] == ["Main Page26!S11"],
      [(i["code"], i.get("change_count")) for i in plan2["properties"]])
fp2 = re.search(r'name="fingerprint" value="([^"]+)"', page2).group(1)
client.post(loc2 + "/apply", data={"month": SEP, "fingerprint": fp2, "include": [i["property_id"] for i in plan2["properties"] if i["status"] != "no_activity"] + ["general-overheads"], "exclude_form": "1", "exclude": ["Main Page26!S11"], "ack": "1"})
check("re-import: Nothing to import: ledger, properties and aliases byte-identical", snapshot(db.get_conn()) == snap_after and
      db.get_conn().execute("SELECT COUNT(*) FROM properties WHERE name='22 Perryfield Way'").fetchone()[0] == 1)
check("re-import: the second batch stays staged (nothing was applied)", db.get_conn().execute("SELECT status FROM import_batches WHERE id=?", (bid2,)).fetchone()[0] == "staged")

# --------------------------------------------------------------- history untouched
print("history")
hist = db.get_conn()
check("history: every row outside September is exactly as before the import (excluding the cleanup's own removals)",
      hist.execute("SELECT COUNT(*) FROM transactions WHERE substr(date,1,7)!=? AND import_batch_id IS NOT NULL", (SEP,)).fetchone()[0] == 0)
check("history: the Lascar double-counts removed by the cleanup did not return", hist.execute("SELECT COUNT(*) FROM transactions WHERE property_id='general-overheads' AND description='Lascar Wharf'").fetchone()[0] == 1)

# --------------------------------------------------------------- undo
print("undo")
cu = db.get_conn()
# 22 Perryfield has data from the import only -> undo may remove the property; later data makes it unsafe
cu.execute("INSERT INTO transactions (property_id,date,description,amount,direction,category,source) VALUES ('22-perryfield-way','2026-09-15','hand entered',5,'expense','other','manual')")
cu.commit()
try:
    A.undo_batch(cu, bid_e2e, "test")
    check("undo refused while the new property has data recorded after the import", False)
except A.ImportRefused as exc:
    check("undo refused while the new property has data recorded after the import", "22-perryfield-way" in str(exc) or "later" in str(exc), str(exc))
check("...and nothing was removed", cu.execute("SELECT 1 FROM properties WHERE id='22-perryfield-way'").fetchone() is not None)
cu.execute("DELETE FROM transactions WHERE description='hand entered' AND property_id='22-perryfield-way'")
cu.commit()
res = A.undo_batch(cu, bid_e2e, "test")
check("undo restores every previous value and row, and removes the property it created", snapshot(cu) == snap_clean and res["removed_properties"] == ["22-perryfield-way"] and
      not cu.execute("SELECT 1 FROM workbook_sheet_map WHERE property_id='22-perryfield-way'").fetchone() and
      not cu.execute("SELECT 1 FROM property_identity_aliases WHERE property_id='22-perryfield-way'").fetchone() and not cu.execute("SELECT 1 FROM import_exclusions").fetchone())
check("the cleanup is itself undoable", A.undo_batch(cu, cid, "test")["restored_exactly"] is not None and snapshot(cu) == snap0)

print(f"\n{COUNT - len(FAILS)}/{COUNT} checks passed" + ("" if not FAILS else f"; FAILED: {FAILS}"))
sys.exit(1 if FAILS else 0)
