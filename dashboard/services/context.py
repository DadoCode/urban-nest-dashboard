"""The canonical global-filter contract every analytical page will share:
?from=YYYY-MM-DD&to=YYYY-MM-DD&property=<id|all>&compare=<choice>

Period "shortcuts" (This month / YTD / ...) aren't their own query param --
they're just links whose href already carries the from/to dates that
shortcut resolves to, computed once here against the current period. That
keeps the URL itself the single source of truth: copy it, and someone else
sees exactly the same filtered view, and Reports (Phase 8) can reuse the
same three params rather than inventing its own.

Only the Overview page reads this today (Phase 2) -- the context bar is
built once here and rendered in base.html, and later phases (3-5) wire
Occupancy/Expenses/the property workspace into it as those pages get
rebuilt, rather than every page inventing its own filter scheme in the
meantime."""
import services.kpis as kpis

SHORTCUTS = ["this_month", "last_month", "ytd", "last12", "custom"]
SHORTCUT_LABELS = {"this_month": "This month", "last_month": "Last month",
                    "ytd": "Year to date", "last12": "Last 12 months", "custom": "Custom"}
COMPARE_CHOICES = ["previous_period", "previous_year", "none"]
COMPARE_LABELS = {"previous_period": "vs previous period", "previous_year": "vs same period last year",
                   "none": "No comparison"}


def _iso_bounds(sy, sm, ey, em):
    start, end_exclusive = kpis.range_bounds(sy, sm, ey, em)
    return start, end_exclusive


def resolve_context(conn, args):
    cy, cm = kpis.current_period(conn)
    from_str, to_str = args.get("from"), args.get("to")
    choice = "this_month"
    partial = True

    if from_str and to_str:
        try:
            sy, sm = map(int, from_str.split("-")[:2])
            ey, em = map(int, to_str.split("-")[:2])
            choice = "custom"
            partial = (ey, em) >= (cy, cm)
        except (ValueError, IndexError):
            sy, sm, ey, em = cy, cm, cy, cm
    else:
        sy, sm, ey, em = cy, cm, cy, cm

    if sy == ey and sm == 1 and em == 12:
        display = str(sy)
    elif sy == ey and sm == em:
        from services.common import MONTH_NAMES
        display = f"{MONTH_NAMES[sm]} {sy}"
    else:
        from services.common import MONTH_ABBR
        display = f"{MONTH_ABBR[sm]} {sy} – {MONTH_ABBR[em]} {ey}"

    property_id = args.get("property") or None
    if property_id == "all":
        property_id = None

    compare = args.get("compare", "previous_period")
    if compare not in COMPARE_CHOICES:
        compare = "previous_period"

    def shortcut_href(key):
        if key == "this_month":
            fsy, fsm, fey, fem = cy, cm, cy, cm
        elif key == "last_month":
            py, pm = kpis.prior_month(cy, cm)
            fsy, fsm, fey, fem = py, pm, py, pm
        elif key == "ytd":
            fsy, fsm, fey, fem = cy, 1, cy, cm
        elif key == "last12":
            fsy, fsm = kpis.add_months(cy, cm, -11)
            fey, fem = cy, cm
        else:  # custom -- keep whatever's currently selected
            fsy, fsm, fey, fem = sy, sm, ey, em
        return {"from": f"{fsy}-{fsm:02d}-01", "to": f"{fey}-{fem:02d}-01"}

    return {
        "choice": choice, "start_year": sy, "start_month": sm, "end_year": ey, "end_month": em,
        "partial": partial, "display": display,
        "from_input": f"{sy}-{sm:02d}", "to_input": f"{ey}-{em:02d}",
        "property_id": property_id, "compare": compare,
        "shortcuts": [{"key": k, "label": SHORTCUT_LABELS[k], "params": shortcut_href(k)} for k in SHORTCUTS],
        "compare_choices": [{"key": k, "label": COMPARE_LABELS[k]} for k in COMPARE_CHOICES],
    }
