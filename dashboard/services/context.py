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
from urllib.parse import parse_qsl, urlencode

import services.kpis as kpis

COOKIE = "un_ctx"
KEYS = ("from", "to", "property", "compare")

SHORTCUTS = ["this_month", "last_month", "ytd", "last12", "custom"]
SHORTCUT_LABELS = {"this_month": "This month", "last_month": "Last month",
                    "ytd": "Year to date", "last12": "Last 12 months", "custom": "Custom"}
COMPARE_CHOICES = ["previous_period", "previous_year", "none"]
COMPARE_LABELS = {"previous_period": "Previous period", "previous_year": "Same period last year",
                   "none": "No comparison"}


def _iso_bounds(sy, sm, ey, em):
    start, end_exclusive = kpis.range_bounds(sy, sm, ey, em)
    return start, end_exclusive


def _display(sy, sm, ey, em):
    from services.common import MONTH_ABBR, MONTH_NAMES
    if sy == ey and sm == 1 and em == 12:
        return str(sy)
    if sy == ey and sm == em:
        return f"{MONTH_NAMES[sm]} {sy}"
    return f"{MONTH_ABBR[sm]} {sy} – {MONTH_ABBR[em]} {ey}"


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

    display = _display(sy, sm, ey, em)

    property_id = args.get("property") or None
    if property_id == "all":
        property_id = None

    compare = args.get("compare", "previous_period")
    if compare not in COMPARE_CHOICES:
        compare = "previous_period"

    if compare == "previous_period":
        compare_display = _display(*kpis.prior_period(sy, sm, ey, em))
    elif compare == "previous_year":
        compare_display = _display(*kpis.same_period_last_year(sy, sm, ey, em))
    else:
        compare_display = None

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

    # "Latest" means the standard default view: current month, every
    # property, the default comparison -- not just the right dates. A
    # property workspace overrides is_latest itself in request_context(),
    # since "property_id is None" isn't a meaningful idea there (it's
    # always fixed to that one property by the page, not by a choice).
    period_is_latest = (sy, sm, ey, em) == (cy, cm, cy, cm)
    is_latest = period_is_latest and property_id is None and compare == "previous_period"
    latest = {"from": f"{cy}-{cm:02d}-01", "to": f"{cy}-{cm:02d}-01", "property": "all", "compare": "previous_period"}
    return {
        "is_latest": is_latest, "period_is_latest": period_is_latest, "latest_params": latest,
        "choice": choice, "start_year": sy, "start_month": sm, "end_year": ey, "end_month": em,
        "partial": partial, "display": display,
        "from_input": f"{sy}-{sm:02d}", "to_input": f"{ey}-{em:02d}",
        "property_id": property_id, "compare": compare, "compare_display": compare_display,
        "shortcuts": [{"key": k, "label": SHORTCUT_LABELS[k], "params": shortcut_href(k)} for k in SHORTCUTS],
        "compare_choices": [{"key": k, "label": COMPARE_LABELS[k]} for k in COMPARE_CHOICES],
    }


def merged_params():
    """(merged, explicit, saved): what the user last chose (cookie) overlaid
    with whatever this URL says. Merging -- rather than "URL replaces
    cookie" -- means a link carrying only some params never silently drops
    the rest of the user's selection."""
    from flask import request
    saved = {k: v for k, v in parse_qsl(request.cookies.get(COOKIE, "")) if k in KEYS}
    explicit = {k: request.args[k] for k in KEYS if request.args.get(k)}
    return {**saved, **explicit}, explicit, saved


def request_context(conn, fixed_property=None):
    """The period / property / compare selection for this request, shared
    by every analytical page. In a property workspace the property is fixed
    to that flat: it is never editable there and never overwrites the
    remembered portfolio-level property choice."""
    from flask import g
    merged, explicit, saved = merged_params()
    if explicit:
        keep = {k: v for k, v in explicit.items() if not (fixed_property and k == "property")}
        g.ctx_save = urlencode({**saved, **keep})
    ctx = resolve_context(conn, merged)
    if fixed_property:
        ctx["property_id"] = fixed_property
        ctx["fixed_property"] = True
        # The property is fixed by the page, not a choice -- "latest" here
        # only means the latest month with the default comparison.
        ctx["is_latest"] = ctx["period_is_latest"] and ctx["compare"] == "previous_period"
    return ctx


def link_params(keep_property=False, **override):
    """Query params that carry the user's current context onto another page."""
    merged, _, _ = merged_params()
    p = {k: v for k, v in merged.items() if keep_property or k != "property"}
    p.update(override)
    return p


def range_params(ctx, **extra):
    """Query params that reproduce this context on another page."""
    p = {"from": ctx["from_input"] + "-01", "to": ctx["to_input"] + "-01",
         "property": ctx["property_id"] or "all", "compare": ctx["compare"]}
    p.update(extra)
    return p


def compare_bounds(ctx):
    """(start, end) ISO bounds of the comparison period, or None."""
    a = (ctx["start_year"], ctx["start_month"], ctx["end_year"], ctx["end_month"])
    if ctx["compare"] == "previous_period":
        return kpis.range_bounds(*kpis.prior_period(*a))
    if ctx["compare"] == "previous_year":
        return kpis.range_bounds(*kpis.same_period_last_year(*a))
    return None
