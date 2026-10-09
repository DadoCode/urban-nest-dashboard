"""Phase 6: connected drilldowns (scratch database; nothing real is touched).

For every connected metric: THE NUMBER CLICKED == THE BREAKDOWN TOTAL SHOWN. Also: workbook-written rows are read-only,
provenance points at the right batch/property block, filters survive drawer/edit/delete, and the Bookings evidence is honest
about monthly totals vs individual reservations.

Run: .venv/bin/python tests/test_drilldowns.py
"""
import json
import os
import re
import sys
import tempfile
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="un-drill-test-"))
os.environ["DASHBOARD_DB_PATH"] = str(TMP / "t.db")
os.environ["DASHBOARD_UPLOADS_PATH"] = str(TMP / "uploads")
os.environ.pop("VERCEL", None)
os.environ.pop("UN_DEMO_MODE", None)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "dashboard"))

import db  # noqa: E402
import services.kpis as kpis  # noqa: E402
from services import completeness  # noqa: E402
from services.drilldown import nights_evidence, revenue_records  # noqa: E402
from routes.expenses import _costs  # noqa: E402

FAILS, COUNT = [], 0


def check(name, cond, detail=""):
    global COUNT
    COUNT += 1
    if not cond:
        FAILS.append(name)
        print(f"  FAIL  {name} {detail}")


def text(resp):
    h = resp.data.decode()
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", re.sub(r"<script.*?</script>", "", h, flags=re.S)))


db.ensure_schema()
c = db.get_conn()
SEP, JUN, OCT = "2026-09", "2026-06", "2026-10"


def prop(pid, name, active=1, fee=None, managed=None, start=None):
    managed = bool(fee) if managed is None else managed
    c.execute("INSERT INTO properties (id, code, name, address, type, active, start_date, management_fee_pct, is_managed) VALUES (?,?,?,?,'flat',?,?,?,?)",
              (pid, pid.upper(), name, name, active, start, fee, 1 if managed else 0))
    completeness.seed_defaults(c, pid)


def tx(pid, date, amount, direction, category, desc="row", source="workbook", batch=None, ref=None, vendor=None):
    cur = c.execute("""INSERT INTO transactions (property_id,date,vendor,description,amount,direction,category,capex,source,import_batch_id,source_ref)
                       VALUES (?,?,?,?,?,?,?,0,?,?,?)""", (pid, date, vendor, desc, amount, direction, category, source, batch, ref))
    return cur.lastrowid


def aggregate(pid, ym, nights, source="workbook", batch=None, ref=None):
    y, m = map(int, ym.split("-"))
    c.execute("""INSERT INTO bookings (property_id, platform, reservation_id, check_in, check_out, gross_revenue, platform_fees, cleaning_fee, net_revenue, status, source, import_batch_id, source_ref)
                 VALUES (?, 'excel', 'monthly-aggregate', ?, ?, 0, 0, 0, 0, 'confirmed', ?, ?, ?)""",
              (pid, f"{ym}-01", f"{ym}-{1 + nights:02d}", source, batch, ref))


prop("op1", "Operated Flat One")                                   # operated, workbook-controlled September
prop("mg1", "Managed Flat One", fee=12.0)                          # managed, recorded fee, REVIEW at import
prop("mg2", "Managed Flat Two", fee=15.0)                          # managed, NO fee row recorded -> estimate
prop("res1", "Reservations Flat")                                  # operated with a real reservation (not workbook)
prop("agg1", "Monthly Total Flat")                                 # older Excel history: the month's revenue sits on its monthly row
c.execute("""INSERT INTO bookings (property_id, platform, reservation_id, check_in, check_out, gross_revenue, platform_fees, cleaning_fee, net_revenue, status, source)
             VALUES ('agg1','excel','monthly-aggregate','2026-09-01','2026-09-16', 0, 0, 0, 700, 'confirmed', 'excel_import')""")
prop("gone", "Gone Flat", active=0)                                # inactive, activity only in June
prop("later", "Later Flat", start="2026-12-01")                    # not started in September
c.execute("INSERT INTO properties (id, code, name, address, type, active) VALUES ('general-overheads','GO','Business Costs','', 'overhead', 1)")
c.execute("INSERT INTO import_batches (id, filename, file_hash, uploaded_at, applied_at, status, period, kind, properties, reconciliation, before_totals, after_totals) VALUES "
          "(7, 'Biz_Accounts_Tracker_2026_Sept_v4.xlsx', 'h7', '2026-10-06 10:00', '2026-10-06 10:05:00', 'applied', ?, 'workbook', ?, ?, '{}', ?)",
          (SEP, json.dumps(["op1", "mg1", "general-overheads"]),
           json.dumps({"op1": [{"metric": "Income", "workbook": 1500.0, "imported": 1500.0, "diff": 0, "status": "PASS"}],
                       "mg1": [{"metric": "Management fee rate", "workbook": 0.1, "imported": 0.12, "diff": -0.02, "status": "REVIEW"}],
                       "general-overheads": [{"metric": "General", "workbook": 80.0, "imported": 80.0, "diff": 0, "status": "PASS"}]}),
           json.dumps({"op1": {}, "mg1": {}, "general-overheads": {}})))

# operated, September: workbook income + costs; one other-income row; one hand-entered cost
inc1 = tx("op1", f"{SEP}-01", 1000.0, "income", "booking_income", "3-6 direct", batch=7, ref="OP126!AC10")
inc2 = tx("op1", f"{SEP}-01", 500.0, "income", "booking_income", "7-9 BDC", batch=7, ref="OP126!AC11")
oth = tx("op1", f"{SEP}-01", 40.0, "income", "other", "late checkout", batch=7, ref="OP126!AC12")
cost_wb = tx("op1", f"{SEP}-05", 200.0, "expense", "cleaning", "cleaners", batch=7, ref="OP126!AC20", vendor="CleanCo")
cost_manual = tx("op1", f"{SEP}-06", 30.0, "expense", "purchase", "bulbs", source="manual", vendor="Shop")
aggregate("op1", SEP, 23, batch=7, ref=f"Days Booked {SEP}")
# managed with a recorded fee (12% of 2000 = 240, recorded 200 => rate 10.0%)
tx("mg1", f"{SEP}-01", 2000.0, "income", "booking_income", "guest income", source="excel_import")
fee_wb = tx("mg1", f"{SEP}-01", 200.0, "expense", "management_fee", "FG Mngmt Fee (12%)", batch=7, ref="MG126!AC19")
tx("mg1", f"{SEP}-09", 60.0, "expense", "cleaning", "mg1 cleaning", batch=7, ref="MG126!AC16")
aggregate("mg1", SEP, 20, batch=7, ref=f"Days Booked {SEP}")
# managed with no fee row: estimated from the configured percentage
tx("mg2", f"{SEP}-01", 1000.0, "income", "booking_income", "guest income", source="excel_import")
aggregate("mg2", SEP, 10, source="excel_import")
# a property with a real reservation
c.execute("""INSERT INTO bookings (property_id, platform, reservation_id, check_in, check_out, gross_revenue, platform_fees, cleaning_fee, net_revenue, status, source)
             VALUES ('res1','airbnb','RES-1','2026-09-10','2026-09-14', 500, 50, 0, 450, 'confirmed', 'manual')""")
# an inactive property that was active in June
tx("gone", f"{JUN}-01", 100.0, "income", "booking_income", "old income", source="excel_import")
aggregate("gone", JUN, 5, source="excel_import")
# business cost (September) and a managed-property cost
tx("general-overheads", f"{SEP}-03", 80.0, "expense", "software", "tools", batch=7, ref="Main Page26!S16")
c.commit()

from app import create_app  # noqa: E402

client = create_app().test_client()
Q = "from=2026-09-01&to=2026-09-01&compare=none"
S0, E0 = kpis.month_bounds(2026, 9)
conn = db.get_conn()

# ------------------------------------------------------------------ A. Overview Urban Nest Revenue drilldown keeps the period
print("A. Overview tiles")
ov = client.get(f"/?{Q}").data.decode()
hrefs = {m.group(2): m.group(1).replace("&amp;", "&") for m in re.finditer(r'href="([^"]+)" title="([^"]+)" class="tile link"', ov)}
rev_href = next((h for t, h in hrefs.items() if "revenue" in t), "")
check("Urban Nest Revenue tile opens Properties with the same period, property scope and compare", rev_href.startswith("/properties?") and "from=2026-09-01" in rev_href and "to=2026-09-01" in rev_href and "compare=none" in rev_href and "property=all" in rev_href, rev_href)
check("the dead sort= parameter is gone", all("sort=" not in h for h in hrefs.values()))
one = client.get(f"/?{Q}&property=op1").data.decode()
check("with one property selected the revenue tile opens that property's Revenue records, not the portfolio list",
      re.search(r'href="(/properties/op1/bookings\?[^"]*)#revenue-records"', one) is not None)
snap = kpis.adjusted_kpi_snapshot(conn, None, S0, E0)
page = text(client.get(rev_href))
m = re.search(r"Urban Nest Revenue £([\d,\.]+) = operated £([\d,\.]+) \+ management fees £([\d,\.]+)", page)
check("Properties summary (whole pounds): Urban Nest Revenue equals the Overview tile and equals operated + management fees to within rounding",
      bool(m) and abs(float(m.group(1).replace(",", "")) - snap["revenue"]) < 0.51
      and abs(float(m.group(2).replace(",", "")) + float(m.group(3).replace(",", "")) - snap["revenue"]) < 1.01, (m.groups() if m else page[:300], snap["revenue"]))
m = re.search(r"Property Profit (−?)£([\d,\.]+) = operated (−?)£([\d,\.]+) \+ management fees £([\d,\.]+)", page)
check("Properties summary: Property Profit equals the Overview tile", bool(m) and abs((-1 if m.group(1) else 1) * float(m.group(2).replace(",", "")) - snap["net_profit"]) < 0.51, (m.groups() if m else None, snap["net_profit"]))

# ------------------------------------------------------------------ B / C / E. Operated Property Costs, Property Costs, Business Costs
print("B/C. Costs drilldowns reconcile")
bar = snap["costs"]
check("Overview 'Operated Property Costs' bar value is the operated properties' costs only (not managed, not business)",
      abs(bar - (200.0 + 30.0)) < 0.005, bar)
check("the chart series and its click are relabelled and aimed at model=operated", "Operated Property Costs" in ov and "params.set('model', 'operated')" in ov and "params.set('scope', 'property')" in ov)
op_exp = client.get(f"/expenses?{Q}&scope=property&model=operated")
op_text = text(op_exp)
check("Expenses ?scope=property&model=operated: headline equals the Overview bar exactly",
      f"Operated Property Costs £{bar:,.0f}" in op_text and abs(_costs(conn, S0, E0, scope="property", model="operated") - bar) < 0.005, op_text[op_text.find("Operated Property Costs"):][:80])
rows_html = op_exp.data.decode()
ledger_amounts = [float(a.replace(",", "")) for a in re.findall(r'<td class="num">£([\d,]+\.\d{2})</td>\s*<td class="note[^"]*">', rows_html)]
check("…and the ledger rows listed there add up to the same number", abs(sum(ledger_amounts) - bar) < 0.005, ledger_amounts)
check("…and it lists no managed-property or business row", "mg1 cleaning" not in rows_html and "tools" not in rows_html)
line = client.get(f"/?{Q}").data.decode()
pc = re.search(r'<a href="([^"]+)" title="[^"]*" aria-label="Property Costs £([\d,]+)[^"]*"', line)
bc = re.search(r'<a href="([^"]+)" title="[^"]*" aria-label="Business Costs £([\d,]+)[^"]*"', line)
prop_total = _costs(conn, S0, E0, scope="property")
bus_total = _costs(conn, S0, E0, scope="business")
check("Overview 'Property Costs · Business Costs' line shows the Expenses figures", bool(pc) and bool(bc) and pc.group(2) == f"{prop_total:,.0f}" and bc.group(2) == f"{bus_total:,.0f}", (pc.groups() if pc else None, bc.groups() if bc else None))
pdest = text(client.get(pc.group(1).replace("&amp;", "&")))
check("Property Costs link: Expenses opens in Property Costs scope, same period, headline equals the clicked figure",
      "scope=property" in pc.group(1) and "from=2026-09-01" in pc.group(1) and f"Property Costs £{prop_total:,.0f}" in pdest, pc.group(1))
bdest = text(client.get(bc.group(1).replace("&amp;", "&")))
check("Business Costs link: Expenses opens in Business scope, same period, headline equals the clicked figure",
      "scope=business" in bc.group(1) and "from=2026-09-01" in bc.group(1) and f"Business Costs £{bus_total:,.0f}" in bdest, bc.group(1))
check("property and business costs stay separate (property total excludes the business row, business view lists only it)", prop_total == 200.0 + 30.0 + 60.0 and bus_total == 80.0)
check("a managed property's cost is in Property Costs but not in the operated bar", abs((prop_total - bar) - 60.0) < 0.005)

# ------------------------------------------------------------------ D / K. Occupancy and Bookings evidence for a monthly-total month
print("D/K. Occupancy and booking evidence")
bk = client.get(f"/properties/op1/bookings?{Q}")
bt = text(bk)
check("Occupancy drilldown states booked nights ÷ available nights", "Occupancy 77% = 23 booked nights ÷ 30 available nights" in bt, bt[bt.find("Occupancy 7"):][:120])
ev = nights_evidence(conn, conn.execute("SELECT * FROM properties WHERE id='op1'").fetchone(), S0, E0)
check("evidence rows add up to the booked-nights KPI", ev["booked"] == ev["kpi_booked"] == 23 and ev["monthly_only"] and abs(ev["occupancy"] - 23 / 30) < 1e-9)
check("workbook month: Reservations tile says monthly totals only, not zero", "Reservations — monthly totals only" in bt, bt[:300])
check("workbook month: never says 'No bookings imported' while showing booked nights and revenue",
      "No bookings imported" not in bt and "Monthly booking totals are available for this period" in bt and "no individual reservation records were imported" in bt)
check("workbook month: the nights evidence names the workbook source (Days Booked, batch)", "Monthly total (Days Booked)" in bt and "Batch #7" in bt and f"Days Booked {SEP}" in bt)
rv = client.get(f"/properties/res1/bookings?{Q}")
rt = text(rv)
check("a month with a real reservation shows it as a reservation (no 'monthly totals only')", "Reservation" in rt and "monthly totals only" not in rt and "No individual reservations imported" not in rt)
check("ADR and RevPAR show their parts", "= £1,500.00 Gross Booking Revenue ÷ 23 booked nights" in bt and "RevPAR" in bt and "÷ 30 available nights" in bt)

# ------------------------------------------------------------------ Revenue records: booking income vs fee; operated vs managed
print("Revenue records")
op = conn.execute("SELECT * FROM properties WHERE id='op1'").fetchone()
mg = conn.execute("SELECT * FROM properties WHERE id='mg1'").fetchone()
r_op, r_mg = revenue_records(conn, op, S0, E0), revenue_records(conn, mg, S0, E0)
check("Gross Booking Revenue records add up to the KPI (operated and managed)", abs(r_op["gross"] - r_op["kpi_gross"]) < 0.005 and abs(r_mg["gross"] - r_mg["kpi_gross"]) < 0.005 and r_op["gross"] == 1500.0)
check("Urban Nest Revenue records (incl. other income) add up to kpis.revenue", abs(r_op["urban_nest_revenue"] - r_op["kpi_revenue"]) < 0.005 and r_op["urban_nest_revenue"] == 1540.0)
check("Management Fee Earned records add up to kpis.business_income (recorded row, not a percentage guess)", r_mg["fee"]["rows"] and abs(r_mg["fee"]["recorded"] - r_mg["fee"]["value"]) < 0.005 and r_mg["fee"]["value"] == 200.0 and not r_mg["fee"]["estimated"])
mt = text(client.get(f"/properties/mg1/bookings?{Q}"))
check("managed page: booking income and the management fee are separate sections; Gross Booking Revenue is never shown as the fee",
      "Gross Booking Revenue £2,000.00" in mt and "Management Fee Earned £200.00" in mt and "The record behind Management Fee Earned" in mt and "Most of it belongs to the property's owner" in mt)
check("managed page: configured % and the recorded rate are both shown (12% configured, 10.0% recorded)", "Configured fee: 12%" in mt and "Recorded fee ÷ Gross Booking Revenue = 10.0%" in mt)
check("managed page: the fee row's workbook source and batch are shown", "Batch #7" in mt and "MG126!AC19" in mt)
m2 = text(client.get(f"/properties/mg2/bookings?{Q}"))
check("managed property with no fee row: says it is an estimate and does not present it as a recorded fee", "No fee row is recorded" in m2 and "estimates it as the configured percentage" in m2 and "£150.00" in m2)
ag = text(client.get(f"/properties/agg1/bookings?{Q}"))
agr = revenue_records(conn, conn.execute("SELECT * FROM properties WHERE id='agg1'").fetchone(), S0, E0)
check("revenue carried on a month's total row is shown as a monthly total (not as individual reservations) and still adds up to the KPI",
      abs(agr["monthly_total"] - 700) < 0.005 and agr["reservations"] == 0 and abs(agr["gross"] - agr["kpi_gross"]) < 0.005 and "Monthly booking total" in ag and "Gross Booking Revenue £700.00" in ag and "listed under Reservations below" not in ag, ag[ag.find("Revenue records"):][:300])
ot = text(client.get(f"/properties/op1/bookings?{Q}"))
check("operated page: booking income, other income and Urban Nest Revenue are distinguished", "Booking income" in ot and "Other income" in ot and "Urban Nest Revenue £1,540.00" in ot and "Gross Booking Revenue £1,500.00" in ot)
check("operated page has no management-fee section", "The record behind Management Fee Earned" not in ot)

# ------------------------------------------------------------------ F. Property Profit breakdown uses the KPI snapshot
print("F. Property Profit breakdown")
ps = kpis.adjusted_kpi_snapshot(conn, "op1", S0, E0)
pov = text(client.get(f"/properties/op1?{Q}"))
m = re.search(r"Revenue £([\d,]+) − Costs £([\d,]+) = £([\d,]+)", pov)
check("operated Property Profit: 'Revenue − Costs = Profit' is the KPI snapshot's own revenue, costs and net profit",
      bool(m) and [int(x.replace(",", "")) for x in m.groups()] == [round(ps["revenue"]), round(ps["costs"]), round(ps["net_profit"])] and abs(ps["revenue"] - ps["costs"] - ps["net_profit"]) < 0.005, (m.groups() if m else pov[:200]))
ph = client.get(f"/properties/op1?{Q}").data.decode()
check("revenue side opens Revenue records and cost side opens this property's Expenses, both with the period", re.search(r'href="/properties/op1/bookings\?[^"]*from=2026-09-01[^"]*#revenue-records"', ph) and re.search(r'href="/properties/op1/expenses\?[^"]*from=2026-09-01', ph))
check("Property Costs tile equals the property Expenses tab total", f"£{_costs(conn, S0, E0, property_id='op1'):,.0f}" in pov and "2 · £230" in text(client.get(f"/properties/op1/expenses?{Q}")).replace("  ", " "), text(client.get(f"/properties/op1/expenses?{Q}"))[:400])
mov = client.get(f"/properties/mg1?{Q}").data.decode()
check("managed overview: Gross Booking Revenue, Management Fee and Occupancy each link to their own evidence",
      all(re.search(a, mov) for a in (r'href="/properties/mg1/bookings\?[^"]*#revenue-records"', r'href="/properties/mg1/bookings\?[^"]*#fees"', r'href="/properties/mg1/bookings\?[^"]*#booked-nights"')))
check("REVIEW from the import shows on the managed fee tile and links to that property's block in that batch", 'href="/imports/7#prop-mg1"' in mov and "REVIEW" in mov)
check("a property the import left clean shows no REVIEW pill", "REVIEW" not in ph)

# ------------------------------------------------------------------ G. Targets
print("G. Targets")
tg = client.get("/targets?month=2026-09").data.decode()
check("Targets rows link to the property's Performance for the same month", re.search(r'href="/properties/op1/performance\?[^"]*from=2026-09-01[^"]*to=2026-09-01', tg) is not None)
td = text(client.get("/targets/op1/edit?month=2026-09"))
hist = kpis.monthly_series(conn, "op1")
sep = next(r for r in hist if r["ym"] == SEP)
check("target drawer shows the existing formula (gross revenue − property costs = Operating Profit) from the KPI series, and says it is not Property Profit",
      f"Gross revenue £{sep['revenue']:,.2f}" in td and f"Operating Profit £{sep['net_profit']:,.2f}" in td and "not the same as Property Profit" in td, td[:500])
check("target drawer links to Performance for that month", "Open Performance for September 2026" in td)

# ------------------------------------------------------------------ H. Inactive / not-started properties
print("H. Inactive and not-started")
pj = client.get("/properties?from=2026-06-01&to=2026-06-01&compare=none").data.decode()
_rows = lambda h: re.findall(r"<tr>\s*<td class=\"c-name\">.*?</tr>", h, re.S)
gone_row = next(r for r in _rows(pj) if "Gone Flat" in r)
check("an inactive property's historical month with activity keeps its drilldowns and says Inactive", "Inactive" in gone_row and "#revenue-records" in gone_row)
po = client.get(f"/properties?from=2026-10-01&to=2026-10-01&compare=none&status=all").data.decode()
gone_oct = next(r for r in _rows(po) if "Gone Flat" in r)
later_oct = next(r for r in _rows(po) if "Later Flat" in r)
check("an inactive property with no activity in the period shows '—' and no metric links", "—" in gone_oct and "mlink" not in gone_oct and "Inactive" in gone_oct)
check("a property that has not started shows 'Not active in this period' and no metric links", "Not active in this period" in later_oct and "mlink" not in later_oct)
gb = text(client.get(f"/properties/later/bookings?{Q}"))
check("not-started property: no misleading occupancy drilldown", "Not active in this period" in gb and "booked nights ÷" not in gb)

# ------------------------------------------------------------------ J. Provenance and read-only workbook rows
print("J. Provenance and read-only rows")
wd = client.get(f"/expenses/transactions/{cost_wb}")
wt = wd.data.decode()
check("workbook row drawer: Imported from, Batch, Source cell and Applied are shown", "Biz Accounts Tracker 2026 Sept v4" in wt and "September 2026" in wt and "#7" in wt and "OP126!AC20" in wt and "2026-10-06 10:05" in wt)
check("workbook row drawer links to the batch AND that property's anchored block", 'href="/imports/7#prop-op1"' in wt)
check("workbook row drawer has no Save/Edit or Delete", "Save changes" not in wt and "Delete transaction" not in wt and "can't be edited or deleted" in wt and "Controlled by workbook import" in wt)
check("management-fee row is labelled as Urban Nest income, not Opex", "Management fee · Urban Nest income" in client.get(f"/expenses/transactions/{fee_wb}").data.decode())
md = client.get(f"/expenses/transactions/{cost_manual}").data.decode()
check("a hand-entered row keeps its Edit and Delete actions and says it was entered by hand", "Save changes" in md and "Delete transaction" in md and "entered by hand" in md.lower())
legacy_id = tx("mg1", f"{SEP}-02", 5.0, "expense", "other", "legacy", source="excel_import")
c.commit()
lg = client.get(f"/expenses/transactions/{legacy_id}").data.decode()
check("an earlier-Excel row says so honestly (no invented batch or cell) and stays editable", "Earlier Excel import" in lg and "Batch" not in lg and "Save changes" in lg)
c.commit()
before = conn.execute("SELECT amount, category FROM transactions WHERE id=?", (cost_wb,)).fetchone()
r = client.post(f"/expenses/transactions/{cost_wb}/edit", data={"property_id": "op1", "vendor": "x", "description": "x", "amount": "1", "category": "other"})
r2 = client.post(f"/expenses/transactions/{cost_wb}/delete")
after = db.get_conn().execute("SELECT amount, category FROM transactions WHERE id=?", (cost_wb,)).fetchone()
check("the server refuses edit and delete of a workbook row even if the form is forged", after is not None and tuple(after) == tuple(before))
bd = client.get(f"/bookings/{conn.execute('SELECT id FROM bookings WHERE property_id=? ', ('op1',)).fetchone()[0]}/drawer").data.decode()
check("booking drawer for a workbook monthly total says so and shows the batch and Days Booked source", "booked nights" in bd and "not an individual reservation" in bd and "#7" in bd and f"Days Booked {SEP}" in bd and 'href="/imports/7#prop-op1"' in bd)
ap = client.get("/imports/7").data.decode()
check("the applied import page has the anchored property block the links point at and opens it from the hash", 'id="prop-mg1"' in ap and 'id="prop-op1"' in ap and "location.hash" in ap)
check("the applied import page links back to each property's Revenue records and Expenses for that month", "/properties/op1/bookings?" in ap and "#revenue-records" in ap)

# ------------------------------------------------------------------ I. Query context survives edit / delete round trips
print("I. Context round trip")
filt = f"/expenses?{Q}&scope=property&t_category=purchase&t_q=bulbs&t_sort=amount&t_dir=asc"
ref = {"Referer": f"http://localhost{filt}#ledger"}
rr = client.post(f"/expenses/transactions/{cost_manual}/edit", data={"property_id": "op1", "vendor": "Shop", "description": "bulbs", "amount": "31", "category": "purchase"}, headers=ref)
check("after editing a row you return to the same filtered ledger (period, scope, category, search, sort)", rr.status_code == 302 and rr.headers["Location"] == filt + "#ledger", rr.headers.get("Location"))
rd = client.post(f"/expenses/transactions/{cost_manual}/delete", headers=ref)
check("after deleting a row you return to the same filtered ledger", rd.status_code == 302 and rd.headers["Location"] == filt + "#ledger", rd.headers.get("Location"))
bad = client.post(f"/expenses/transactions/{cost_wb}/delete", headers={"Referer": "https://evil.example/expenses?x=1"})
check("a foreign referrer is never followed", bad.status_code == 302 and "evil.example" not in bad.headers["Location"])
rt2 = client.get(f"/expenses?{Q}&t_batch=7&property=all")
check("expenses filtered to a batch shows that import's expense rows only, with the batch chip", "Import #7" in text(rt2) and "cleaners" in text(rt2) and "bulbs" not in text(rt2))
chain = client.get("/expenses?from=2026-09-01&to=2026-09-01&compare=none&scope=property&model=operated&t_category=cleaning").data.decode()
chips = re.findall(r'class="chip" href="([^"]+)"', chain)
chips = [h.replace("&amp;", "&") for h in chips]
check("filters chain: every chip keeps the period", len(chips) == 2 and all("from=2026-09-01" in h and "to=2026-09-01" in h for h in chips), chips)
check("removing the model chip returns to all Property Costs and keeps the category filter", "model=" not in chips[0] and "scope=property" in chips[0] and "t_category=cleaning" in chips[0], chips[0])
check("removing the category chip keeps the operated model", "model=operated" in chips[1] and "t_category" not in chips[1], chips[1])
check("scope switch links never carry a model", all("model=" not in h for h in re.findall(r'<a class="(?:on)?" href="([^"]+)">(?:All|Property Costs|Business Costs)</a>', chain)))

# ------------------------------------------------------------------ Bookings > Performance totals row and links
print("Portfolio occupancy drilldown")
bp = client.get(f"/bookings/performance?{Q}").data.decode()
bpt = text(client.get(f"/bookings/performance?{Q}"))
tot_nights, tot_avail = kpis.booked_nights(conn, None, S0, E0), kpis.available_nights(conn, None, S0, E0)
check("portfolio line equals the KPI (booked ÷ available nights = the Overview occupancy)", f"Portfolio {kpis.occupancy(conn, None, S0, E0) * 100:.0f}% {tot_nights} {tot_avail}" in bpt, bpt[bpt.find("Portfolio"):][:120])
rows_n = [int(a) for a, _b in re.findall(r'<td class="num">(\d+)</td>\s*<td class="num">(\d+)</td>\s*<td class="num hide-sm">', bp)][:-1]     # last match is the Portfolio line
check("the property rows add up to the portfolio booked nights", sum(rows_n) == tot_nights, (rows_n, tot_nights))
check("the property names open that property's booked-nights evidence with the period", re.search(r'href="/properties/op1/bookings\?[^"]*from=2026-09-01[^"]*#booked-nights"', bp) is not None)

# ------------------------------------------------------------------ REVIEW is judged against the CURRENT configuration
print("REVIEW follows the current fee configuration")
from services.provenance import review_status  # noqa: E402
check("fee-rate REVIEW stored at import is still REVIEW while the configured % disagrees (12% vs 10.0% recorded)", review_status(conn, "mg1", SEP, SEP) == ("REVIEW", 7))
c.execute("UPDATE properties SET management_fee_pct=10.0 WHERE id='mg1'"); c.commit()
check("…and resolves to PASS once the configuration follows the workbook's rate (no pill on the page any more)",
      review_status(db.get_conn(), "mg1", SEP, SEP) == ("PASS", 7) and "REVIEW" not in client.get(f"/properties/mg1?{Q}").data.decode())
c.execute("UPDATE properties SET management_fee_pct=12.0 WHERE id='mg1'"); c.commit()
check("a property with no workbook import has no verdict at all", review_status(db.get_conn(), "mg2", SEP, SEP) is None)

# ------------------------------------------------------------------ responsive hooks
print("Responsive hooks")
css = (Path(__file__).resolve().parent.parent / "dashboard" / "static" / "style.css").read_text()
check("tab bars, import detail cards and legends scroll inside themselves on narrow screens instead of widening the page", ".tabs, .sidebar { overflow-x: auto;" in css and "details.card { overflow-x: auto; }" in css)
check("secondary columns collapse below 760px and metric links keep a focus ring", ".hide-sm { display: none; }" in css and "a.mlink:focus-visible" in css and ".cost-line a:focus-visible" in css)

print(f"\n{COUNT - len(FAILS)}/{COUNT} checks passed" + ("" if not FAILS else f"; FAILED: {FAILS}"))
sys.exit(1 if FAILS else 0)
