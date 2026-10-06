"""Completion-pass tests: canonical full names and aliases, the NW4 model + start date, Lascar Wharf double counting, a
confirmed separate expense (Crescent B. Ads), new-property creation (22 Perryfield at 15%), not-active periods, Draycott,
and the end-to-end re-import / undo.

Scratch database seeded like the real one (current names, models, history); the user's workbook is read-only here and
nothing touches data/dashboard.db.

Run: .venv/bin/python tests/test_workbook_properties.py
"""
import hashlib
import html as htmllib
import io
import json
import os
import re
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
from services import completeness, kpis  # noqa: E402
from services.workbook import apply as A  # noqa: E402
from services.workbook import batches as B  # noqa: E402
from services.workbook import cleanup as K  # noqa: E402
from services.workbook import config as C  # noqa: E402
from services.workbook import identity  # noqa: E402
from services.workbook import plan as P  # noqa: E402

FAILS, COUNT = [], 0


def check(name, cond, detail=""):
    global COUNT
    COUNT += 1
    if not cond:
        FAILS.append(name)
        print(f"  FAIL  {name} {detail}")


db.ensure_schema()
conn = db.get_conn()
# names, models and ids exactly as the real dashboard has them today (before the clean-up)
SEED = [("11-perryfield-way", "11PW", "11 Perryfield Way", 10.0), ("170-miles-building", "170E", "170 Miles Building", 10.0),
        ("175-miles-building", "175E", "175 Miles Building", 10.0), ("19-draycott-ave", "19Draycott", "19 Draycott Ave", 12.0),
        ("44-spooner-road", "S10", "44 Spooner Road", None), ("campbell-hill-w8", "W8", "7A Campden Hill", 15.0),
        ("crested-court", "CC", "40 Crested Court", 15.0), ("lascar-wharf", "LW", "602 Lascar Wharf", None),
        ("nw4", "NW4", "Flat 3 NW4", 15.0), ("tottenham-court-road", "TCR", "Tottenham Court Road", 15.0)]
for pid, code, name, fee in SEED:
    conn.execute("INSERT INTO properties (id, code, name, address, type, management_fee_pct) VALUES (?,?,?,?, 'flat', ?)", (pid, code, name, name, fee))
conn.execute("INSERT INTO properties (id, code, name, address, type) VALUES ('general-overheads','OVERHEAD','Portfolio General Expenses','-','overhead')")
identity.seed(conn)
for pid, *_rest in SEED:
    completeness.seed_defaults(conn, pid)          # the real database has these for every flat
conn.commit()
CANON = {pid: v[0] for pid, v in C.CANONICAL_NAMES.items()}


def tx(pid, date, desc, amount, direction="expense", cat="other", source="excel_import"):
    conn.execute("INSERT INTO transactions (property_id,date,description,amount,direction,category,source) VALUES (?,?,?,?,?,?,?)",
                 (pid, date, desc, amount, direction, cat, source))


# Lascar Wharf: months of property costs, and the same total echoed as a BUSINESS row (the known double count)
for m, (rent, other) in enumerate([(2550, 569.02), (2550, 1003.01), (2550, 1174.15)], 1):
    tx("lascar-wharf", f"2026-{m:02d}-01", "LW rent", rent, cat="rent")
    tx("lascar-wharf", f"2026-{m:02d}-01", "Cleaning", other, cat="cleaning")
    tx("general-overheads", f"2026-{m:02d}-01", "Lascar Wharf", rent + other, cat="rent")
tx("general-overheads", "2026-04-01", "Lascar Wharf", 111.11, cat="rent")      # does NOT equal LW's costs that month: must be left alone
tx("lascar-wharf", "2026-04-01", "LW rent", 2550, cat="rent")
tx("general-overheads", "2026-09-01", "General", 3030.0)
# NW4: July / August income that predates its start date: must survive untouched
tx("nw4", "2026-07-01", "29 - 2", 5284.27, "income", "booking_income")
tx("nw4", "2026-08-01", "2 - 5", 3483.97, "income", "booking_income")
conn.commit()
SEP = "2026-09"


def snapshot(c):
    parts = []
    for table, order in (("transactions", "id"), ("bookings", "id"), ("booking_source_state", "property_id, month"), ("properties", "id"),
                         ("property_identity_aliases", "alias_norm"), ("workbook_sheet_map", "sheet_code"), ("import_exclusions", "period, source_ref"),
                         ("import_distinct", "period, label_norm, amount")):
        parts.append(json.dumps([tuple(r) for r in c.execute(f"SELECT * FROM {table} ORDER BY {order}")], default=str))
    return hashlib.sha256("|".join(parts).encode()).hexdigest()


def stage(data, c=None):
    c = c or db.get_conn()
    bid = B.stage(c, data, "Biz_Accounts_Tracker_2026_Sept_v4.xlsx")
    row, parsed = B.load(c, bid)
    return c, bid, parsed


def item(plan, code):
    return next(i for i in plan["properties"] if i["code"] == code)


def text(resp):
    return htmllib.unescape(resp.data.decode())


# --------------------------------------------------------------- identity
print("identity, aliases and names")
check("resolve: exact id", identity.resolve(conn, "lascar-wharf") == ("lascar-wharf", "id"))
check("resolve: exact alias (LW)", identity.resolve(conn, "LW")[0] == "lascar-wharf")
check("resolve: normalised alias ('lascar  WHARF')", identity.resolve(conn, "lascar  WHARF") == ("lascar-wharf", "normalised alias"))
check("resolve: sheet code mapping (W8)", identity.resolve(conn, "W8")[0] == "campbell-hill-w8")
check("resolve: unknown text is never guessed", identity.resolve(conn, "Lasker Wharff") == (None, None))
check("resolve: the workbook's spelling '7A Campbell Hill' reaches 7A Campden Hill Road", identity.resolve(conn, "7A Campbell Hill")[0] == "campbell-hill-w8")
check("resolve: 'Shaldon Mansions' and 'Flat 3' reach Tottenham Court Road and NW4", identity.resolve(conn, "Shaldon Mansions")[0] == "tottenham-court-road" and identity.resolve(conn, "Flat 3")[0] == "nw4")
check("resolve: Forest Gate is NOT matched to anything (no certain mapping)", identity.resolve(conn, "29 Station Road, Forest Gate, London, E7 0ES") == (None, None) and identity.resolve(conn, "Forest Gate") == (None, None))
check("every sheet code is stored permanently", {r[0] for r in conn.execute("SELECT sheet_code FROM workbook_sheet_map")} >= {"CC", "LW", "W8", "NW4", "TCR", "S10", "170E", "175E", "11PW", "19Draycott"})
table = K.mapping_table(conn)
by = {r["property_id"]: r for r in table}
check("mapping table: every canonical full name you gave", {pid: by[pid]["canonical"] for pid in CANON} == CANON and
      by["nw4"]["canonical"] == "Flat 3, 48 Station Road, NW4 3SX" and by["19-draycott-ave"]["canonical"] == "Flat 1, 19 Draycott Avenue, Chelsea, London, SW3 3BS")
check("mapping table: NW4 operated and active from 2026-09-01", "managed -> operated" in by["nw4"]["model"] and by["nw4"]["start_date"] == "2026-09-01")
check("mapping table: 22 Perryfield is to be created managed 15%", by["22-perryfield-way"]["model"].startswith("managed 15%") and by["22-perryfield-way"]["current_name"].startswith("(not in"))
check("mapping table: 44 Spooner Road is flagged (no full address given; Forest Gate not matched)", by["44-spooner-road"]["question"] and "Forest Gate" in by["44-spooner-road"]["question"])

# --------------------------------------------------------------- cleanup (scratch)
print("cleanup: full names, NW4 model + start date, Lascar double count, Crescent decision")
before_ids = {r[0] for r in conn.execute("SELECT id FROM properties")}
tx_counts = {pid: conn.execute("SELECT COUNT(*) FROM transactions WHERE property_id=?", (pid,)).fetchone()[0] for pid in before_ids}
snap0 = snapshot(conn)
acts = K.plan_cleanup(conn)
check("plan: every property except 44 Spooner Road is renamed to its full name", {a["property_id"]: a["to"] for a in acts["renames"]} == {p: n for p, n in CANON.items() if p != "44-spooner-road" and p != "22-perryfield-way"}, [a["property_id"] for a in acts["renames"]])
check("plan: NW4 managed -> operated", [(a["property_id"], a["from"], a["to"]) for a in acts["models"]] == [("nw4", "managed", "operated")])
check("plan: NW4 start date 2026-09-01", [(a["property_id"], a["to"]) for a in acts["start_dates"]] == [("nw4", "2026-09-01")])
check("plan: Crescent B. Ads is recorded as a separate expense", [(a["period"], a["label"], a["amount"]) for a in acts["distinct"]] == [("2026-09", "Crescent B. Ads", 3000.0)])
check("plan: exactly the 3 Lascar months equal to the penny are confirmed duplicates; the odd row is left alone", len(acts["duplicates"]) == 3 and len(acts["left_alone"]) == 1)
impact = {r["month"]: r for r in K.model_impact(conn, "nw4", None)}
check("NW4 impact on history: fee estimate disappears, operated profit = revenue - costs", impact["2026-07"]["before"]["management_fee"] > 0 and impact["2026-07"]["after"]["management_fee"] == 0)
check("impact preview wrote nothing", snapshot(conn) == snap0)
cid = K.apply_cleanup(conn, acts, "test")
check("renames keep every id and every record (display-name cleanup only)", {r[0] for r in conn.execute("SELECT id FROM properties")} == before_ids and
      all(conn.execute("SELECT COUNT(*) FROM transactions WHERE property_id=?", (pid,)).fetchone()[0] == tx_counts[pid] for pid in before_ids if pid not in ("general-overheads", "lascar-wharf")))
check("every property now carries its full canonical name AND address", all(conn.execute("SELECT name, address FROM properties WHERE id=?", (pid,)).fetchone()[:2] == (CANON[pid], CANON[pid])
      for pid in CANON if pid not in ("44-spooner-road", "22-perryfield-way")))
check("old names and short codes still resolve (aliases drive matching)", identity.resolve(conn, "19 Draycott Ave")[0] == "19-draycott-ave" and identity.resolve(conn, "602 Lascar Wharf")[0] == "lascar-wharf" and
      identity.resolve(conn, "NW4")[0] == "nw4" and identity.resolve(conn, "Flat 40, Crested Court, 3 Shearwater Drive, London, NW9 7AD")[0] == "crested-court" and identity.resolve(conn, "TCR")[0] == "tottenham-court-road")
check("44 Spooner Road is untouched", conn.execute("SELECT name FROM properties WHERE id='44-spooner-road'").fetchone()[0] == "44 Spooner Road")
check("NW4 is operated with start date 2026-09-01; no NW4 transaction was deleted", conn.execute("SELECT management_fee_pct, start_date FROM properties WHERE id='nw4'").fetchone()[:2] == (None, "2026-09-01") and
      conn.execute("SELECT COUNT(*) FROM transactions WHERE property_id='nw4'").fetchone()[0] == tx_counts["nw4"])
check("only the 3 confirmed Lascar business rows were removed", conn.execute("SELECT COUNT(*) FROM transactions WHERE property_id='general-overheads' AND description='Lascar Wharf'").fetchone()[0] == 1)
check("a second cleanup finds nothing to do", all(not v for k, v in K.plan_cleanup(conn).items() if k in ("renames", "models", "duplicates", "start_dates", "distinct")))
r = A.undo_batch(conn, cid, "test")
check("undoing the cleanup restores the database exactly (names, model, start date, rows, decisions)", snapshot(conn) == snap0)
cid = K.apply_cleanup(conn, K.plan_cleanup(conn), "test")        # the state every later test works from
snap_clean = snapshot(conn)

# --------------------------------------------------------------- not active: NW4 before 1 Sep 2026
print("start date: NOT ACTIVE before a property joined")
c, bid, parsed = stage(ORIGINAL)
for ym in ("2026-07", "2026-08"):
    pl = P.plan_month(c, parsed, ym, with_after=False)
    nw = item(pl, "NW4")
    check(f"NW4 {ym} is NOT ACTIVE: no flags, nothing imported or removed, not 'missing'", nw["status"] == "not_active" and not nw["checks"] and not nw["rows"] and
          "NOT ACTIVE" in nw["reasons"][0] and not any("blank" in r for r in nw["reasons"]), nw["status"])
check("NW4 August: the workbook's pre-start rows are reported as not imported, not invented", "not imported" in item(P.plan_month(c, parsed, "2026-08", with_after=False), "NW4")["reasons"][0])
check("the existing NW4 July / August rows are left exactly as they are", c.execute("SELECT COUNT(*) FROM transactions WHERE property_id='nw4' AND substr(date,1,7) IN ('2026-07','2026-08')").fetchone()[0] == 2)
check("month overview ignores NW4 before it joined", not any("Flat 3" in n for m in P.month_overview(c, parsed) if m["ym"] in ("2026-07", "2026-08") for n in m["properties"]))
jul = ("2026-07-01", "2026-08-01")
h = completeness.health_for(c, "nw4", *jul)
check("data health: NW4 in July is 'Not active until 2026-09-01', nothing is missing", h["not_active"] and h["missing"] == [] and completeness.health_state(h, "July") == ("neutral", "Not active until 2026-09-01"))
check("data health: NW4 in September is judged normally", not completeness.health_for(c, "nw4", "2026-09-01", "2026-10-01").get("not_active"))
check("occupancy availability: NW4 has 0 available nights in August, 30 in September, 14 for 15 Aug - 15 Sep",
      kpis.available_nights(c, "nw4", "2026-08-01", "2026-09-01") == 0 and kpis.available_nights(c, "nw4", "2026-09-01", "2026-10-01") == 30 and
      kpis.available_nights(c, "nw4", "2026-08-15", "2026-09-15") == 14)
check("portfolio availability excludes a property before it joined (9 flats in August, 10 in September)", kpis.available_nights(c, None, "2026-08-01", "2026-09-01") == 9 * 31 and
      kpis.available_nights(c, None, "2026-09-01", "2026-10-01") == 10 * 30)
check("a property with no start date is unaffected (always available)", kpis.available_nights(c, "crested-court", "2026-08-01", "2026-09-01") == 31)
plan = P.plan_month(c, parsed, SEP)
nw = item(plan, "NW4")
check("NW4 September: active, operated, no 'Management model' flag, reconciles", nw["status"] == "ok" and not any(ch["metric"].startswith("Management") for ch in nw["checks"]), nw["status"])
check("NW4 preview: workbook / current / after all shown", nw["workbook"]["property_costs"] == 6519.46 and nw["after"]["property_costs"] == 6519.46 and nw["current"]["management_fee"] == 0.0)

# --------------------------------------------------------------- business: Crescent B. Ads is a separate, genuine marketing cost
print("business costs: Crescent B. Ads")
biz = plan["business"]
cres = [s for s in biz["suspects"] if s["label"].startswith("Crescent")]
check("Crescent B. Ads is still shown as matching NW4's Sourcing Fee, but as a CONFIRMED separate expense (not flagged)", cres and cres[0]["distinct"] and not biz.get("flagged") and biz["status"] == "ok", biz["status"])
check("its check reads PASS: 'Separate expense'", any(ch["metric"] == "Separate expense: Crescent B. Ads" and ch["status"] == "PASS" for ch in biz["checks"]))
want_rows = P.desired_business(parsed, SEP)["rows"]
cr = [r for r in want_rows if r["description"].startswith("Crescent")]
check("it is imported as a Business Cost in the marketing category, amount 3000", len(cr) == 1 and cr[0]["category"] == "marketing" and cr[0]["amount"] == 3000.0 and cr[0]["property_id"] == "general-overheads")
check("NW4's own Sourcing Fee is a separate property capex row (both kept)", any(r["description"] == "Sourcing Fee" and r["capex"] == 1 and r["amount"] == 3000.0 for r in P.desired_property(parsed, "NW4", SEP, "nw4")["rows"]))
plan_aug = P.plan_month(c, parsed, "2026-08", with_after=False)
check("other (undecided) matches are still flagged: August TCR Linens / Pillows", {"TCR Linens", "TCR Pillows/Protector"} <= {s["label"] for s in plan_aug["business"]["suspects"] if not s["distinct"]} and plan_aug["business"]["status"] == "review")

# --------------------------------------------------------------- new property: 22 Perryfield Way (managed, 15%)
print("new property creation (22 Perryfield Way, managed 15%)")
p22 = item(plan, "22PW")
np_ = p22["new_property"]
check("22PW proposal: full name, id, sheet, aliases", p22["status"] == "new_property" and np_["name"] == "Flat 22, Eider Apartments, 73 Perryfield Way, London, NW9 7FD" and
      np_["property_id"] == "22-perryfield-way" and np_["sheet"] == "22PW26" and {"22PW", "22 Perryfield Way", "Flat 22", "Eider Apartments Flat 22"} <= set(np_["aliases"]), np_)
check("22PW proposal: managed at 15% (your decision), but still NOT created or confirmed", np_["model"] == "managed" and np_["pct"] == 15.0 and np_["confidence"] == "confirmed" and not np_["confirmed"]
      and not c.execute("SELECT 1 FROM properties WHERE id='22-perryfield-way'").fetchone())
sel = {i["property_id"] for i in plan["properties"] if i["status"] == "ok"} | {"22-perryfield-way"}
try:
    A.apply_batch(c, bid, plan, sel, "test")
    check("22PW: service-level creation is refused until confirmed", False)
except A.ImportRefused as exc:
    check("22PW: service-level creation is refused until confirmed", "Confirm" in str(exc))
check("...and the refusal wrote nothing", snapshot(c) == snap_clean)

from app import create_app  # noqa: E402

client = create_app().test_client()
up = client.post("/imports/upload", data={"workbook": (io.BytesIO(ORIGINAL), "Biz_Accounts_Tracker_2026_Sept_v4.xlsx")}, content_type="multipart/form-data")
loc = up.headers["Location"]
bid_e2e = int(loc.rsplit("/", 1)[1])
page = text(client.get(loc))
check("page: NEW PROPERTY DETECTED with name, model and fee fields pre-filled (managed, 15)", "NEW PROPERTY DETECTED" in page and 'name="new_model:22-perryfield-way"' in page and
      re.search(r'name="new_pct:22-perryfield-way" value="15(\.0)?"', page) is not None and "Flat 22, Eider Apartments, 73 Perryfield Way" in page)
check("page: four-column figures (current dashboard / workbook / after import / change)", "Current dashboard" in page and "After import" in page and ">Workbook<" in page)
check("page: full names with the short code secondary", "Flat 602, Lascar Wharf Building, 21 Parnham Street, London, E14 7FN" in page and "· LW" in page)
check("page: NW4 July / August are not listed as a problem and Crescent is shown as a separate expense", "Separate expense" in page)
fp = re.search(r'name="fingerprint" value="([^"]+)"', page).group(1)
ticked = re.findall(r'name="include" value="([^"]+)" checked', page)
check("page: the new property is NOT pre-ticked; the clean properties and business costs are", "22-perryfield-way" not in ticked and "general-overheads" in ticked and "crested-court" in ticked, ticked)
n_props = c.execute("SELECT COUNT(*) FROM properties").fetchone()[0]
client.post(loc + "/apply", data={"month": SEP, "fingerprint": fp, "include": ticked + ["22-perryfield-way"], "exclude_form": "1",
                                  "new_name:22-perryfield-way": np_["name"], "new_model:22-perryfield-way": "managed", "new_pct:22-perryfield-way": ""})
check("page: a CLEARED fee box is not silently replaced by the default: refused, nothing created", c.execute("SELECT COUNT(*) FROM properties").fetchone()[0] == n_props)
fresh = db.get_conn()
client.post(loc + "/apply", data={"month": SEP, "fingerprint": fp, "include": ticked + ["22-perryfield-way", "19-draycott-ave"], "exclude_form": "1", "ack": "1",
                                  "new_name:22-perryfield-way": np_["name"], "new_model:22-perryfield-way": "managed", "new_pct:22-perryfield-way": "15"})
row = fresh.execute("SELECT * FROM import_batches WHERE id=?", (bid_e2e,)).fetchone()
check("e2e: the import was applied", row["status"] == "applied", row["status"])
p = fresh.execute("SELECT * FROM properties WHERE id='22-perryfield-way'").fetchone()
check("22PW created: stable id, full name AND address, MANAGED at 15%, flat, no start date invented",
      p and p["name"] == np_["name"] and p["address"] == np_["name"] and p["management_fee_pct"] == 15.0 and p["type"] == "flat" and p["start_date"] is None, dict(p) if p else None)
check("22PW: sheet mapping and every alias saved; each resolves to it", fresh.execute("SELECT property_id FROM workbook_sheet_map WHERE sheet_code='22PW'").fetchone()[0] == "22-perryfield-way" and
      all(identity.resolve(fresh, a)[0] == "22-perryfield-way" for a in ("22PW", "22 Perryfield Way", "Flat 22", "Eider Apartments Flat 22", "22 perryfield way", np_["name"])))
check("22PW: September data imported and the batch records the new property and counters", fresh.execute("SELECT COUNT(*) FROM transactions WHERE property_id='22-perryfield-way' AND import_batch_id=?", (bid_e2e,)).fetchone()[0] >= 1
      and json.loads(row["new_properties"]) == ["22-perryfield-way"] and row["rows_added"] > 100)
check("no duplicate property: exactly one Perryfield 22 and one of every other", fresh.execute("SELECT COUNT(*) FROM properties WHERE id LIKE '22-perry%'").fetchone()[0] == 1 and fresh.execute("SELECT COUNT(*) FROM properties").fetchone()[0] == n_props + 1)
check("Crescent B. Ads landed as a business marketing row; NW4's sourcing fee as NW4 capex",
      fresh.execute("SELECT category, amount FROM transactions WHERE property_id='general-overheads' AND description='Crescent B. Ads' AND date LIKE '2026-09%'").fetchone()[:2] == ("marketing", 3000.0) and
      fresh.execute("SELECT amount, capex FROM transactions WHERE property_id='nw4' AND description='Sourcing Fee' AND date LIKE '2026-09%'").fetchone()[:2] == (3000.0, 1))

# --------------------------------------------------------------- pages show full names everywhere
print("pages and full names")
names = {pid: n for pid, n in fresh.execute("SELECT id, name FROM properties WHERE type='flat'")}
for url in ("/properties", "/properties/22-perryfield-way", "/expenses?from=2026-09-01&to=2026-09-01&t_scope=22-perryfield-way", "/bookings", "/reports", "/reconciliation",
            "/documents", "/targets", "/", f"/imports/{bid_e2e}", "/properties/nw4", "/properties/nw4/documents", "/properties/lascar-wharf/settings"):
    resp = client.get(url)
    check(f"page {url.split('?')[0]} loads", resp.status_code == 200, resp.status_code)
for k in ("/properties", "/expenses?from=2026-09-01&to=2026-09-01", "/documents", "/reports", "/targets", "/bookings"):
    t = text(client.get(k))
    shown = [pid for pid, n in names.items() if n in t]
    check(f"{k.split('?')[0]}: property names shown are the full names ({len(shown)})", len(shown) >= 1 and (k != "/properties" or len(shown) == len(names)), len(shown))
html = " ".join(text(client.get(u)) for u in ("/", "/properties", "/expenses", "/bookings", "/documents", "/targets", "/reconciliation", f"/imports/{bid_e2e}"))
check("QA: no page uses a bare short code as a property label", not re.findall(r">\s*(NW4|LW|CC|TCR|W8|S10|11PW|22PW|19Draycott)\s*<", html))
check("QA: the old short names ('19 Draycott Ave', 'Flat 3 NW4', '40 Crested Court') are no longer shown as property names", not any(o in text(client.get("/properties")) for o in (">19 Draycott Ave<", ">Flat 3 NW4<", ">40 Crested Court<")))
check("Properties page lists 22 Perryfield by its full name; the expense filter offers it", names["22-perryfield-way"] in text(client.get("/properties")) and 'value="22-perryfield-way"' in text(client.get("/expenses?from=2026-09-01&to=2026-09-01")))
hp = client.get("/properties/nw4/health?from=2026-07-01&to=2026-07-01")
check("NW4 data-health drawer in July says 'Not active yet', not 'missing'", hp.status_code == 200 and "Not active yet" in text(hp) and "Missing" not in text(hp), hp.status_code)
hs = client.get("/properties/nw4/health?from=2026-09-01&to=2026-09-01")
check("NW4 data-health drawer in September is judged normally", hs.status_code == 200 and "Not active yet" not in text(hs))
settings = text(client.get("/properties/lascar-wharf/settings"))
check("settings show the workbook identity (code LW, aliases incl. the old name)", "sheet code" in settings and "LW" in settings and "602 Lascar Wharf" in settings)

# --------------------------------------------------------------- reconciliation of trusted rows
print("reconciliation")
parsed_e = B.load(fresh, bid_e2e)[1]
for code in ("LW", "CC", "TCR", "NW4", "22PW", "W8", "170E", "175E", "11PW"):
    v = A.verify_item(fresh, parsed_e, code, SEP)
    ok = all(r_["status"] == "PASS" or not r_["gating"] for r_ in v["rows"])
    check(f"WORKBOOK = LEDGER = DASHBOARD for {code} (blank controls with nothing imported do not fail it)", ok, [r_ for r_ in v["rows"] if r_["gating"]])
nwv = P.dashboard_view(fresh, "nw4", SEP)
check("NW4 September (operated): revenue 2868.64, costs 6519.46, NO fee, Property Profit -3650.82 (= workbook net)",
      abs(nwv["revenue"] - 2868.64) < 0.01 and abs(nwv["property_costs"] - 6519.46) < 0.01 and nwv["management_fee"] == 0 and abs(nwv["profit"] - (-3650.82)) < 0.01 and nwv["model"] == "operated", nwv)
v22 = P.dashboard_view(fresh, "22-perryfield-way", SEP)
check("22 Perryfield September is a managed property: costs 8.36, fee 0.00 (no income)", v22["model"] == "managed" and abs(v22["property_costs"] - 8.3575) < 0.01 and v22["management_fee"] == 0)
dv = A.verify_item(fresh, parsed_e, "19Draycott", SEP)
check("Draycott: income / costs / days are NO CONTROL, never PASS", {r_["metric"]: r_["status"] for r_ in dv["rows"]}["Income / booking revenue"] == "NO CONTROL")
dr_plan = item(P.plan_month(fresh, parsed_e, SEP, with_after=False), "19Draycott")
metrics = {c_["metric"]: c_["status"] for c_ in dr_plan["checks"]}
check("Draycott stays REVIEW (reversal, 23.5% fee vs 12%, block total, income without nights) and nothing was corrected",
      dr_plan["status"] == "review" and metrics.get("Reversal of last month's costs") == "REVIEW" and metrics.get("Management fee rate") == "REVIEW" and metrics.get("Detail rows vs block totals") == "REVIEW" and
      abs((fresh.execute("SELECT SUM(amount) FROM transactions WHERE property_id='19-draycott-ave' AND direction='expense' AND date LIKE '2026-09%' AND category!='management_fee' AND source='workbook'").fetchone()[0] or 0) - (-156.3775)) < 0.001, metrics)

# --------------------------------------------------------------- re-import: nothing to import
print("re-import")
snap_after = snapshot(db.get_conn())
up2 = client.post("/imports/upload", data={"workbook": (io.BytesIO(ORIGINAL), "Biz_Accounts_Tracker_2026_Sept_v4.xlsx")}, content_type="multipart/form-data")
loc2 = up2.headers["Location"]
page2 = text(client.get(loc2))
check("re-import preview: no new property is proposed", "NEW PROPERTY DETECTED</strong>" not in page2)
c2 = db.get_conn()
bid2 = int(loc2.rsplit("/", 1)[1])
parsed2 = B.load(c2, bid2)[1]
plan2 = P.plan_month(c2, parsed2, SEP)
check("re-import: every property and business costs have nothing to change", all(i.get("change_count", 0) == 0 for i in plan2["properties"] if i["status"] in ("ok", "unchanged", "review")) and plan2["business"]["change_count"] == 0,
      [(i["code"], i.get("change_count")) for i in plan2["properties"]])
fp2 = re.search(r'name="fingerprint" value="([^"]+)"', page2).group(1)
client.post(loc2 + "/apply", data={"month": SEP, "fingerprint": fp2, "include": [i["property_id"] for i in plan2["properties"] if i["status"] not in ("no_activity", "not_active")] + ["general-overheads"], "exclude_form": "1", "ack": "1"})
check("re-import: Nothing to import: ledger, properties and aliases byte-identical; no second property", snapshot(db.get_conn()) == snap_after and
      db.get_conn().execute("SELECT COUNT(*) FROM properties WHERE id LIKE '22-perry%'").fetchone()[0] == 1)
check("re-import: the second batch stays staged", db.get_conn().execute("SELECT status FROM import_batches WHERE id=?", (bid2,)).fetchone()[0] == "staged")
for ym in ("2026-07", "2026-08"):
    check(f"re-import {ym}: NW4 still not active, still not flagged", item(P.plan_month(c2, parsed2, ym, with_after=False), "NW4")["status"] == "not_active")

# --------------------------------------------------------------- history untouched, undo
print("history and undo")
hist = db.get_conn()
check("history: nothing outside September carries an import batch; the Lascar double-counts did not return",
      hist.execute("SELECT COUNT(*) FROM transactions WHERE substr(date,1,7)!=? AND import_batch_id IS NOT NULL", (SEP,)).fetchone()[0] == 0 and
      hist.execute("SELECT COUNT(*) FROM transactions WHERE property_id='general-overheads' AND description='Lascar Wharf'").fetchone()[0] == 1)
cu = db.get_conn()
cu.execute("INSERT INTO transactions (property_id,date,description,amount,direction,category,source) VALUES ('22-perryfield-way','2026-09-15','hand entered',5,'expense','other','manual')")
cu.commit()
try:
    A.undo_batch(cu, bid_e2e, "test")
    check("undo refused while the new property has data recorded after the import", False)
except A.ImportRefused as exc:
    check("undo refused while the new property has data recorded after the import", "22-perryfield-way" in str(exc) or "later" in str(exc), str(exc))
check("...and the property is still there", cu.execute("SELECT 1 FROM properties WHERE id='22-perryfield-way'").fetchone() is not None)
cu.execute("DELETE FROM transactions WHERE description='hand entered' AND property_id='22-perryfield-way'")
cu.commit()
res = A.undo_batch(cu, bid_e2e, "test")
check("undo restores every previous value and row and removes the property it created (and its aliases / mapping)", snapshot(cu) == snap_clean and res["removed_properties"] == ["22-perryfield-way"] and
      not cu.execute("SELECT 1 FROM workbook_sheet_map WHERE property_id='22-perryfield-way'").fetchone() and
      not cu.execute("SELECT 1 FROM property_identity_aliases WHERE property_id='22-perryfield-way'").fetchone())
check("the cleanup (names, NW4 model + start date, Crescent decision, Lascar rows) is itself undoable back to the original", A.undo_batch(cu, cid, "test") is not None and snapshot(cu) == snap0)

print(f"\n{COUNT - len(FAILS)}/{COUNT} checks passed" + ("" if not FAILS else f"; FAILED: {FAILS}"))
sys.exit(1 if FAILS else 0)
