"""The three meanings of "active", kept apart (scratch database; nothing real is touched).

  A. not started yet           start_date is after the period            -> "Not active until <date>"
  B. active now                properties.active = 1                     -> always expected, always available
  C. historically active, now inactive (active = 0)                      -> still listed and counted in the months it recorded
                                                                           activity; nothing is "missing" or zero-performance today

Run: .venv/bin/python tests/test_active_status.py
"""
import datetime
import os
import sys
import tempfile
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="un-active-test-"))
os.environ["DASHBOARD_DB_PATH"] = str(TMP / "t.db")
os.environ["DASHBOARD_UPLOADS_PATH"] = str(TMP / "uploads")
os.environ.pop("VERCEL", None)
os.environ.pop("UN_DEMO_MODE", None)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "dashboard"))

import db  # noqa: E402
from services import completeness, kpis  # noqa: E402
from services.common import get_properties  # noqa: E402

FAILS, COUNT = [], 0


def check(name, cond, detail=""):
    global COUNT
    COUNT += 1
    if not cond:
        FAILS.append(name)
        print(f"  FAIL  {name} {detail}")


db.ensure_schema()
c = db.get_conn()


def prop(pid, name, active=1, start=None, end=None, fee=None):
    c.execute("INSERT INTO properties (id, code, name, address, type, active, start_date, end_date, management_fee_pct, is_managed) VALUES (?,?,?,?,'flat',?,?,?,?,?)",
              (pid, pid.upper(), name, name, active, start, end, fee, 1 if fee else 0))
    completeness.seed_defaults(c, pid)


def income(pid, ym, amount=100.0):
    c.execute("INSERT INTO transactions (property_id,date,description,amount,direction,category,source) VALUES (?,?,'x',?,'income','booking_income','excel_import')", (pid, f"{ym}-01", amount))


prop("live", "Live flat")                                       # B active, no data in some months
prop("gone", "Gone flat", active=0)                             # C inactive, activity only in 2026-06
prop("never", "Never active", active=0)                         # C inactive, no history at all
prop("late", "Late start", start="2026-09-01")                  # A starts 1 Sep 2026
prop("ended", "Ended flat", active=1, end=("2026-06-30"))       # active flag but an explicit end date
income("live", "2026-06"); income("gone", "2026-06"); income("late", "2026-09"); income("ended", "2026-06")
c.commit()
D = datetime.date.fromisoformat

print("listing: strict / period / default")
ids = lambda rows: sorted(r["id"] for r in rows)
check("strict = active now only", ids(get_properties(c, include_overhead=False, strict=True)) == ["ended", "late", "live"])
check("period containing activity of an inactive property lists it too", "gone" in ids(get_properties(c, include_overhead=False, period=("2026-06-01", "2026-07-01"))))
check("a period without its activity does not", "gone" not in ids(get_properties(c, include_overhead=False, period=("2026-09-01", "2026-10-01"))))
check("an inactive property with no history at all is never listed (until you ask for inactive)", "never" not in ids(get_properties(c, include_overhead=False)))
check("default = active now + inactive ones that have any history (selectable in a filter)", ids(get_properties(c, include_overhead=False)) == ["ended", "gone", "late", "live"])

print("availability")
av = lambda pid, a, b: kpis.available_nights(c, pid, a, b)
check("B active: every night from the start (no start date = always)", av("live", "2026-06-01", "2026-07-01") == 30 and av("live", "2026-10-01", "2026-11-01") == 31)
check("A not started: 0 before the start date, full from it, partial across it", av("late", "2026-08-01", "2026-09-01") == 0 and av("late", "2026-09-01", "2026-10-01") == 30 and av("late", "2026-08-15", "2026-09-15") == 14)
check("C inactive: available only in the month it recorded activity (June), not in July or today", av("gone", "2026-06-01", "2026-07-01") == 30 and av("gone", "2026-07-01", "2026-08-01") == 0 and av("gone", "2026-10-01", "2026-11-01") == 0)
check("C inactive: a range across months counts only the active-history months (June of May-July)", av("gone", "2026-05-01", "2026-08-01") == 30)
check("C inactive with no history: nothing, ever", av("never", "2026-01-01", "2027-01-01") == 0)
check("an explicit end date bounds a property (ended 30 Jun: all of June, none of July)", av("ended", "2026-06-01", "2026-07-01") == 30 and av("ended", "2026-07-01", "2026-08-01") == 0 and av("ended", "2026-06-15", "2026-07-15") == 16)
check("portfolio availability = live + ended(June) + gone(June) in June (late not started)", av(None, "2026-06-01", "2026-07-01") == 30 * 3)
check("portfolio availability in September = live + late (the inactive ones and the ended one drop out)", av(None, "2026-09-01", "2026-10-01") == 30 * 2)
check("portfolio availability in October = live + late", av(None, "2026-10-01", "2026-11-01") == 31 * 2)

print("data health")
why = lambda pid, a, b: completeness.not_active(c, pid, a, b)
check("A: 'Not active until 2026-09-01' for a period before the start", why("late", "2026-08-01", "2026-09-01")["kind"] == "not_started" and why("late", "2026-08-01", "2026-09-01")["text"] == "Not active until 2026-09-01")
check("A: expected normally from the start month", why("late", "2026-09-01", "2026-10-01") is None)
check("B: a normal active property is always expected to produce data", why("live", "2026-06-01", "2026-07-01") is None and why("live", "2026-12-01", "2027-01-01") is None)
check("C: an inactive property is 'Inactive' in every period (no missing documents), including months it had data", why("gone", "2026-06-01", "2026-07-01")["text"] == "Inactive" and why("gone", "2026-10-01", "2026-11-01")["text"] == "Inactive")
h = completeness.health_for(c, "gone", "2026-10-01", "2026-11-01")
check("C: health has nothing missing and says Inactive", h["missing"] == [] and completeness.health_state(h, "October") == ("neutral", "Inactive"))
check("ended: 'Inactive' after the end date, normal before it", why("ended", "2026-07-01", "2026-08-01")["text"] == "Inactive" and why("ended", "2026-06-01", "2026-07-01") is None)
check("B active with no documents IS still flagged (inactive logic must not hide real gaps)", completeness.health_state(completeness.health_for(c, "live", "2026-10-01", "2026-11-01"), "October")[1] != "Inactive")

print("pages")
from app import create_app  # noqa: E402

client = create_app().test_client()
june = client.get("/properties?from=2026-06-01&to=2026-06-01").data.decode()
sept = client.get("/properties?from=2026-09-01&to=2026-09-01").data.decode()
inactive = client.get("/properties?status=inactive").data.decode()
check("June: the count is the ACTIVE properties plus a separate note for the inactive one with activity", "3 active properties + 1 inactive with activity in this period" in june, june[june.find("active propert") - 20:june.find("active propert") + 80] if "active propert" in june else "")
check("September: only active properties are counted, no inactive note", "3 active properties" in sept and "inactive with activity" not in sept)
check("the Inactive filter lists the inactive properties (2) regardless of period", "2 inactive propert" in inactive and "Gone flat" in inactive and "Never active" in inactive)
check("the business cost centre is never counted as a property", "Portfolio" not in sept.split("prop-table")[1] if "prop-table" in sept else True)

print(f"\n{COUNT - len(FAILS)}/{COUNT} checks passed" + ("" if not FAILS else f"; FAILED: {FAILS}"))
sys.exit(1 if FAILS else 0)
