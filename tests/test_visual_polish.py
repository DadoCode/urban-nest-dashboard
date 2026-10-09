"""Phase 7: visual polish -- structural checks only (scratch database; nothing real is touched; no pixel tests).

What must stay true after the polish: the import comparison reads Before / After / Change per area, a rate is a percentage and never money,
REVIEW is open while PASS and NO CONTROL are collapsed (and NO CONTROL does not look like REVIEW), Undo comes after the data, "Active" is quiet text,
inactive / not-started properties are never plotted as 0% occupancy, the property is named once in its workspace, compact names are display-only,
and the Phase 6 drilldown links are all still there.

Run: .venv/bin/python tests/test_visual_polish.py
"""
import json
import os
import re
import sys
import tempfile
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="un-polish-test-"))
os.environ["DASHBOARD_DB_PATH"] = str(TMP / "t.db")
os.environ["DASHBOARD_UPLOADS_PATH"] = str(TMP / "uploads")
os.environ.pop("VERCEL", None)
os.environ.pop("UN_DEMO_MODE", None)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "dashboard"))

import db  # noqa: E402
from services import completeness  # noqa: E402
from services.common import short_name  # noqa: E402

FAILS, COUNT = [], 0


def check(name, cond, detail=""):
    global COUNT
    COUNT += 1
    if not cond:
        FAILS.append(name)
        print(f"  FAIL  {name} {detail}")


db.ensure_schema()
c = db.get_conn()
SEP, JUN = "2026-09", "2026-06"


def prop(pid, name, active=1, fee=None, start=None):
    c.execute("INSERT INTO properties (id, code, name, address, type, active, start_date, management_fee_pct, is_managed) VALUES (?,?,?,?,'flat',?,?,?,?)",
              (pid, pid.upper(), name, name, active, start, fee, 1 if fee else 0))
    completeness.seed_defaults(c, pid)


def tx(pid, date, amount, direction, category, desc="row", source="workbook", batch=None, ref=None):
    return c.execute("INSERT INTO transactions (property_id,date,description,amount,direction,category,capex,source,import_batch_id,source_ref) VALUES (?,?,?,?,?,?,0,?,?,?)",
                     (pid, date, desc, amount, direction, category, source, batch, ref)).lastrowid


def aggregate(pid, ym, nights, source="workbook", batch=None):
    c.execute("INSERT INTO bookings (property_id, platform, reservation_id, check_in, check_out, gross_revenue, platform_fees, cleaning_fee, net_revenue, status, source, import_batch_id, source_ref) "
              "VALUES (?, 'excel', 'monthly-aggregate', ?, ?, 0, 0, 0, 0, 'confirmed', ?, ?, ?)", (pid, f"{ym}-01", f"{ym}-{1 + nights:02d}", source, batch, f"Days Booked {ym}" if batch else None))


prop("op1", "Flat 602, Lascar Wharf Building, 21 Parnham Street, London, E14 7FN")
prop("mg1", "Flats 7 & 8, Shaldon Mansions, 132 Charing Cross Road, London, WC2H 0LA", fee=12.0)
prop("gone", "House 44, Spooner Road, Sheffield, S10 5BN", active=0)
prop("later", "Flat 3, 48 Station Road, NW4 3SX", start="2026-12-01")
c.execute("INSERT INTO properties (id, code, name, address, type, active) VALUES ('general-overheads','GO','Business Costs','', 'overhead', 1)")
rate_check = {"metric": "Management fee rate", "workbook": 0.1, "imported": 0.12, "diff": -0.02, "status": "REVIEW"}
c.execute("INSERT INTO import_batches (id, filename, file_hash, uploaded_at, applied_at, status, period, kind, properties, reconciliation, before_totals, after_totals) VALUES "
          "(7, 'Biz_Accounts_Tracker_2026_Sept_v4.xlsx', 'abcdef0123456789', '2026-10-06 10:00', '2026-10-06 10:05:00', 'applied', ?, 'workbook', ?, ?, ?, ?)",
          (SEP, json.dumps(["op1", "mg1"]),
           json.dumps({"op1": [{"metric": "Income", "workbook": 1500.0, "imported": 1500.0, "diff": 0.0, "status": "PASS"},
                               {"metric": "Days booked", "workbook": 23, "imported": 23, "diff": 0, "status": "PASS"},
                               {"metric": "Occupancy", "workbook": 0.7667, "imported": 0.7667, "diff": 0.0, "status": "PASS"},
                               {"metric": "Operating profit (control)", "workbook": None, "imported": 1270.0, "diff": None, "status": "NO CONTROL"}],
                       "mg1": [{"metric": "Income", "workbook": 2000.0, "imported": 2000.0, "diff": 0.0, "status": "PASS"}, rate_check,
                               {"metric": "Capex", "workbook": None, "imported": 0.0, "diff": None, "status": "NO CONTROL"}]}),
           json.dumps({"op1": {"revenue": 1500.0, "property_costs": 230.0, "profit": 1270.0, "days": 23, "occupancy": 0.7667, "total_expenses": 230.0},
                       "mg1": {"revenue": 1800.0, "property_costs": 60.0, "management_fee": 200.0, "profit": 200.0, "days": 20, "occupancy": 0.6667, "total_expenses": 260.0}}),
           json.dumps({"op1": {"revenue": 1500.0, "property_costs": 230.0, "profit": 1270.0, "days": 23, "occupancy": 0.7667, "total_expenses": 230.0},
                       "mg1": {"revenue": 2000.0, "property_costs": 60.0, "management_fee": 240.0, "profit": 240.0, "days": 20, "occupancy": 0.6667, "total_expenses": 300.0}})))
tx("op1", f"{SEP}-01", 1500.0, "income", "booking_income", "3-6 direct", batch=7, ref="OP126!AC10")
tx("op1", f"{SEP}-05", 230.0, "expense", "cleaning", "cleaners", batch=7, ref="OP126!AC20")
aggregate("op1", SEP, 23, batch=7)
tx("mg1", f"{SEP}-01", 2000.0, "income", "booking_income", "guest income", source="excel_import")
tx("mg1", f"{SEP}-01", 200.0, "expense", "management_fee", "FG Mngmt Fee (12%)", batch=7, ref="MG126!AC19")
aggregate("mg1", SEP, 20, batch=7)
tx("gone", f"{JUN}-01", 100.0, "income", "booking_income", "old income", source="excel_import")
aggregate("gone", JUN, 5, source="excel_import")
c.commit()

from app import create_app  # noqa: E402

client = create_app().test_client()
Q = "from=2026-09-01&to=2026-09-01&compare=none"


def text(resp):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", re.sub(r"<script.*?</script>", "", resp.data.decode(), flags=re.S)))


# ------------------------------------------------------------------ inactive / not-started are never plotted as 0%
print("occupancy charts")
perf = client.get(f"/bookings/performance?{Q}").data.decode()
ranked = json.loads(re.search(r"const rankedLabels = (\[.*?\]);", perf).group(1))
scatter = re.search(r"const scatterPoints = (\[.*?\])\.filter", perf, re.S).group(1)
check("September: an inactive property with no activity is not in the ranked bar chart (no 0% bar)", "Spooner" not in " ".join(ranked) and "Station Road" not in " ".join(ranked), ranked)
check("…nor in the scatter data", "Spooner" not in scatter and "NW4" not in scatter)
check("…it is listed in the table as a dash with the reason, using its full name for the link", re.search(r"House 44, Spooner Road, Sheffield, S10 5BN.*?· Inactive", perf, re.S) is not None)
check("…and a property that has not started says 'Not active in this period'", re.search(r"NW4 3SX.*?· Not active in this period", perf, re.S) is not None)
check("properties that were active stay plotted (compact names in the chart, canonical in the table)", any("Lascar" in x for x in ranked) and any("Shaldon" in x for x in ranked) and "Flat 602, Lascar Wharf Building, 21 Parnham Street" in perf)
jun = client.get("/bookings/performance?from=2026-06-01&to=2026-06-01&compare=none").data.decode()
jun_ranked = json.loads(re.search(r"const rankedLabels = (\[.*?\]);", jun).group(1))
check("June: the inactive property DID have activity, so it stays in the ranking (history is not rewritten)", any("Spooner" in x for x in jun_ranked), jun_ranked)
heat = re.findall(r"<tr>\s*<td style=\"white-space:nowrap\"><a [^>]*>([^<]+)</a></td>", perf)
check("the heatmap keeps a row for a property that recorded a month in the window (history), blank where it recorded nothing", any("Spooner" in h for h in heat), heat)

# ------------------------------------------------------------------ import page: Before / After / Change
print("applied import")
ap = client.get("/imports/7").data.decode()
apt = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", ap))
check("comparison reads Area · metric / Before / After / Change", all(h in apt for h in ("Area · metric", "Before", "After", "Change")) and "Before and after" in apt)
check("each area is one labelled group with its model and its verdict (PASS / REVIEW), not two unlabelled lines", "Operated" in apt and "Managed" in apt and 'class="grp-status"' in ap and "Top line: before" not in apt)
check("shows the figures that apply: operated has Property profit, managed has Management fee; neither shows a meaningless cell",
      "Property profit" in apt and "Management fee" in apt and ap.count("Property profit") >= 1)
check("secondary metrics sit behind 'All metrics' unless they changed", 'data-toggle-rows="cmp"' in ap and 'class="more"' in ap)
check("a change is signed and a no-change is quiet", "+£40.00" in apt and 'class="zero"' in ap)
rev_open = re.search(r'id="prop-mg1".*?</div>\s*(?=<div class="recon|</section>)', ap, re.S).group(0)
check("REVIEW checks are in the open block (a table, not hidden in a collapsed section)", "Management fee rate" in rev_open and "<details" not in rev_open.split("Management fee rate")[0][-400:])
check("the fee rate is a percentage with percentage-point difference, never money", "10%" in apt and "12%" in apt and "−2 pp" in apt and "£0.10" not in apt and "£0.12" not in apt and "−£0.02" not in apt)
check("PASS is collapsed with a quiet tick count", re.search(r'<details class="sub"><summary><span class="rc-pass-text">✓ \d+ checks? passed', ap) is not None)
check("NO CONTROL is collapsed, neutral, and says it is not a failure", 'details class="sub nc"' in ap and "no comparison available, not a failure" in apt and 'class="rc rc-nc"' in ap)
check("NO CONTROL does not share REVIEW's look; PASS, REVIEW, NO CONTROL each have their own class", all(k in ap for k in ("rc rc-pass", "rc rc-review", "rc rc-nc")))
check("Undo comes after the data and the reconciliation", ap.index("Undo import") > ap.index("Reconciliation") > ap.index("Before and after"))
check("the anchored block each drilldown points at still exists", 'id="prop-mg1"' in ap and 'id="prop-op1"' in ap )
check("numbers are right-aligned and the zero difference recedes", 'class="num"' in ap and "zero" in ap)

# ------------------------------------------------------------------ status system
print("status")
pr = client.get(f"/properties?{Q}").data.decode()
check("a normal active row is quiet text, not a green pill", '<span class="st">Active</span>' in pr and 'pill pos">Active' not in pr)
po = client.get("/properties?from=2026-12-01&to=2026-12-01&compare=none&status=all").data.decode()
po2 = client.get(f"/properties?{Q}&status=all").data.decode()
check("exceptions keep a pill: Inactive, and Not active in this period", 'class="pill" title="No longer active' in po2 and "Not active in this period" in po2)
check("Properties columns are unchanged (Property / Model / Revenue-or-Fee / Profit for operated / Occupancy / Status)",
      all(h in pr for h in ("Urban Nest Revenue", "Property Profit", "Management Fee Earned", "Occupancy", ">Status<", ">Model<", ">Property<")))
check("both tables share one column grid (the managed gap column keeps Occupancy and Status aligned)", pr.count("<colgroup>") == 2 and 'class="c-metric c-gap"' in pr)
check("the summary line is whole pounds with the exact figure on hover", re.search(r"<strong title=\"£[\d,]+\.\d{2}\">Urban Nest Revenue £[\d,]+</strong>", pr) is not None)
check("REVIEW sits beside the figure it explains and links to that area's block", 'href="/imports/7#prop-mg1"' in pr)

# ------------------------------------------------------------------ workspace identity + compact names
print("workspace header and compact names")
ws = client.get(f"/properties/op1?{Q}").data.decode()
check("the property is named once in its workspace: one title, no duplicate chip in the context bar", ws.count('class="ws-title"') == 1 and "context-fixed" not in ws)
check("identity line: model · status (and the fee for managed)", "Operated" in text(client.get(f"/properties/op1?{Q}")) and re.search(r'class="ws-meta">Operated<span class="sep">·</span><span>Active</span>', ws) is not None)
wm = client.get(f"/properties/mg1?{Q}").data.decode()
check("managed identity line shows the fee", re.search(r'class="ws-meta">Managed<span class="sep">·</span>12% fee<span class="sep">·</span><span>Active</span>', wm) is not None)
check("an inactive property's identity says so as a flag", 'class="ws-flag">Inactive' in client.get(f"/properties/gone?{Q}").data.decode())
check("first KPI tile label carries no period (the period is in the context bar), so values line up", "Urban Nest Revenue — " not in ws and "Gross Booking Revenue — " not in wm)
check("the full canonical name is always in the page (title, screen readers, ws-addr); the compact form is display-only", "Flat 602, Lascar Wharf Building, 21 Parnham Street, London, E14 7FN" in ws and short_name("Flat 602, Lascar Wharf Building, 21 Parnham Street, London, E14 7FN") == "Flat 602, Lascar Wharf Building")
check("short_name keeps what tells properties apart",
      [short_name(n) for n in ("Flat 3, 48 Station Road, NW4 3SX", "Flat 1, 19 Draycott Avenue, Chelsea, London, SW3 3BS", "7A Campden Hill Road, London, W8 7DX", "House 44, Spooner Road, Sheffield, S10 5BN", "4 Ashford Mews")]
      == ["Flat 3, 48 Station Road", "Flat 1, 19 Draycott Avenue", "7A Campden Hill Road", "House 44, Spooner Road", "4 Ashford Mews"])
check("URLs and slugs are untouched by display names", "/properties/op1" in client.get(f"/properties?{Q}").data.decode() and "/properties/lascar" not in client.get(f"/properties?{Q}").data.decode())

# ------------------------------------------------------------------ drilldowns survive
print("Phase 6 links remain")
bk = client.get(f"/properties/mg1/bookings?{Q}").data.decode()
check("Properties page still links revenue / fee / occupancy to their evidence", all(re.search(a, pr) for a in (r'href="/properties/op1/bookings\?[^"]*#revenue-records"', r'href="/properties/mg1/bookings\?[^"]*#fees"', r'href="/properties/op1/bookings\?[^"]*#booked-nights"')))
check("property Overview tiles still link to their evidence", re.search(r'href="/properties/op1/bookings\?[^"]*#revenue-records"', ws) is not None and re.search(r'href="/properties/op1/expenses\?', ws) is not None)
check("Bookings tab still has Revenue records, Management fee and Booked nights with source links", all(k in bk for k in ('id="revenue-records"', 'id="fees"', 'id="booked-nights"', "/imports/7#prop-mg1")))
ov = client.get(f"/?{Q}").data.decode()
check("Overview still has the Property / Business Costs line and the Operated Property Costs drilldown", 'class="cost-line"' in ov and "params.set('model', 'operated')" in ov)
check("targets still open Performance, drawers still show provenance", "/performance?" in client.get("/targets?month=2026-09").data.decode())
tid = c.execute("SELECT id FROM transactions WHERE import_batch_id=7 LIMIT 1").fetchone()[0]
dr = client.get(f"/expenses/transactions/{tid}").data.decode()
check("drawer: record first, then provenance grouped under 'Controlled by workbook import', read-only and intentional",
      dr.index("Classification") < dr.index("Controlled by workbook import") < dr.index("Imported from") and "Save changes" not in dr and 'class="ro-note"' in dr)

# ------------------------------------------------------------------ imports list + tables
print("lists and tables")
il = text(client.get("/imports"))
check("imports list: period first, a kind label, and a file label that does not truncate to an identical prefix", "September 2026" in il and "#7 · Workbook" in il)
ex = client.get(f"/expenses?{Q}").data.decode()
check("expenses ledger: dates never wrap; source is compact", re.search(r'class="nw">2026-09-\d\d</td>', ex) is not None and "Workbook #7" in ex, repr(ex[ex.find("cleaners") - 250: ex.find("cleaners") + 500] if "cleaners" in ex else "cleaners row not in ledger"))
check("expenses ledger filter bar is one labelled form", 'class="filter-bar ledger-filters"' in ex and 'aria-label="Search vendor or description"' in ex)
tg = client.get("/targets?month=2026-09").data.decode()
check("targets: Performance link is in the actions column, not inside the name", 'class="t-perf"' in tg and 'class="targets-table"' in tg)

# ------------------------------------------------------------------ accessibility hooks
css = (Path(__file__).resolve().parent.parent / "dashboard" / "static" / "style.css").read_text()
check("focus rings on metric links, tile links, ⓘ and the new summaries; icon colour is a real token", all(k in css for k in ("a.mlink:focus-visible", ".tile.link:focus-visible", ".info-dot:focus-visible", "details.sub > summary:focus-visible", "--icon:")))
check("a figure that opens records has a hanging chevron (so numbers stay aligned) and underlines on hover, not by default", "a.mlink::after" in css and "a.mlink:hover { color: var(--brand-ink); text-decoration: underline;" in css)

# ------------------------------------------------------------------ markup: a link never contains a link
print("markup")
from html.parser import HTMLParser  # noqa: E402


class _Nest(HTMLParser):
    def __init__(self):
        super().__init__()
        self.depth, self.bad = 0, 0

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self.depth += 1
            self.bad += self.depth > 1

    def handle_endtag(self, tag):
        if tag == "a":
            self.depth = max(0, self.depth - 1)


def nested_links(url):
    p = _Nest()
    p.feed(client.get(url).data.decode())
    return p.bad


pages = [f"/?{Q}", f"/properties?{Q}", f"/properties/mg1?{Q}", f"/properties/op1?{Q}", f"/properties/mg1/bookings?{Q}", f"/properties/mg1/performance?{Q}", "/imports/7", "/imports",
         f"/expenses?{Q}", f"/bookings/performance?{Q}", "/targets?month=2026-09"]
check("no page nests a link inside a link (a REVIEW flag beside a clickable tile, a metric link in a row link...)", all(nested_links(u) == 0 for u in pages), [u for u in pages if nested_links(u)])
mov = client.get(f"/properties/mg1?{Q}").data.decode()
check("the managed fee tile with a REVIEW flag keeps both links: the tile to the fee record, the flag to the import block",
      re.search(r'class="tile-main"[^>]*>', mov) is not None and 'href="/imports/7#prop-mg1"' in mov and 'class="note tile-flag"' in mov)

print(f"\n{COUNT - len(FAILS)}/{COUNT} checks passed" + ("" if not FAILS else f"; FAILED: {FAILS}"))
sys.exit(1 if FAILS else 0)
