"""Read the monthly workbook into plain data. No database access here.

Rules:
  * Sheet NAMES are listed straight from the zip's workbook.xml; a credential-type
    sheet is excluded by name and its contents are never requested.
  * Only allowlisted sheets are opened, each bounded to the area the layout uses.
  * Blocks are found by their labels and month headers, never by fixed row numbers,
    because the same block sits on different rows in different property sheets.
  * Every problem is recorded as an issue (error | review | info); nothing is guessed.
"""
import datetime
import hashlib
import html
import io
import re
import zipfile

import openpyxl
from openpyxl.utils import get_column_letter

from . import config as C

MONTH_NAMES = ["january", "february", "march", "april", "may", "june", "july",
               "august", "september", "october", "november", "december"]
_MONTH_LOOKUP = {n: i + 1 for i, n in enumerate(MONTH_NAMES)}
_MONTH_LOOKUP.update({"sept": 9, **{n[:3]: i + 1 for i, n in enumerate(MONTH_NAMES)}})

SUMMARY_FIELDS = {"net profit": "net", "operating profit": "operating", "income": "income",
                  "total costs": "total_costs", "opex": "opex", "capex": "capex",
                  "occupancy": "occupancy", "days booked": "days"}


class WorkbookError(Exception):
    """The file cannot be read as a workbook at all."""


# ----------------------------------------------------------------- small helpers

def month_number(text):
    return _MONTH_LOOKUP.get(str(text).strip().lower().rstrip(".")) if isinstance(text, str) else None


def ym(year, month):
    return f"{year}-{month:02d}"


def render_label(value):
    """A label as text. Excel turns a typed "4-9" into a date; render it back as the
    day-month the person typed (m-d), so the stay label survives."""
    if isinstance(value, (datetime.datetime, datetime.date)):
        return f"{value.month}-{value.day}"
    if value is None:
        return ""
    return str(value).strip()


def _is_number(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


class Issues(list):
    def add(self, level, code, message, scope=None, ref=None, month=None):
        self.append({"level": level, "code": code, "message": message, "scope": scope, "ref": ref, "month": month})


class Sheet:
    """Cells of one sheet: {(row, col): (cached_value, formula_or_None)}."""

    def __init__(self, name, cached_rows, formula_rows):
        self.name = name
        self.cells = {}
        self.blank_results = set()          # formulas that WERE calculated and gave an empty string (not "never calculated")
        formulas = {}
        for r, row in enumerate(formula_rows, 1):
            for c, cell in enumerate(row, 1):
                v = getattr(cell, "value", None)
                if isinstance(v, str) and v.startswith("="):
                    formulas[(r, c)] = v
        for r, row in enumerate(cached_rows, 1):
            for c, cell in enumerate(row, 1):
                v = getattr(cell, "value", None)
                if v is not None and not (isinstance(v, str) and v.strip() == ""):
                    self.cells[(r, c)] = (v, formulas.get((r, c)))
                elif (r, c) in formulas and getattr(cell, "data_type", None) == "str":
                    self.blank_results.add((r, c))   # cached as an empty string: calculated, just blank
        for key, f in formulas.items():          # formula with no cached result
            if key not in self.blank_results:
                self.cells.setdefault(key, (None, f))
        self.max_row = max((r for r, _ in self.cells), default=0)

    def value(self, r, c):
        return self.cells.get((r, c), (None, None))[0]

    def formula(self, r, c):
        return self.cells.get((r, c), (None, None))[1]

    def text(self, r, c):
        v = self.value(r, c)
        return v.strip() if isinstance(v, str) else v

    def lower(self, r, c):
        v = self.value(r, c)
        return v.strip().lower() if isinstance(v, str) else None

    def label(self, r, c):
        """A label as lower-case text; a numeric label (170, 175) counts as its text."""
        v = self.value(r, c)
        if isinstance(v, str):
            return v.strip().lower()
        if _is_number(v):
            return str(int(v)) if float(v).is_integer() else str(v)
        return None

    def ref(self, r, c):
        return f"{self.name}!{get_column_letter(c)}{r}"


# --------------------------------------------------------------- sheet roles

def sheet_names(data):
    """Names only, from workbook.xml. Nothing inside any sheet is read here."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            xml = z.read("xl/workbook.xml").decode("utf-8", "replace")
    except (zipfile.BadZipFile, KeyError) as exc:
        raise WorkbookError("This is not an .xlsx workbook.") from exc
    return [html.unescape(m) for m in re.findall(r"<sheet\b[^>]*?\bname=\"([^\"]+)\"", xml)]


def classify(names, codes=None):
    """-> (year, roles) where roles[name] = (role, detail). role: blocked | ignored | main | breakdown | property | candidate.
    `codes` are the sheet codes already mapped to a property; a '<code><yy>' sheet outside them is a CANDIDATE new property."""
    codes = set(codes) if codes is not None else set(C.PROPERTY_SHEETS)
    roles, mains = {}, []
    for n in names:
        low = n.strip().lower()
        base_low = low[:-2] if low[-2:].isdigit() else low
        if C.is_blocked(n):
            roles[n] = ("blocked", "excluded by name; never opened")
        elif low in C.IGNORED_SHEETS or base_low.strip() in C.IGNORED_SHEETS:
            roles[n] = ("ignored", C.IGNORED_SHEETS.get(low) or C.IGNORED_SHEETS[base_low.strip()])
        elif re.fullmatch(re.escape(C.MAIN_SHEET_BASE.lower()) + r"\d{2}", low):
            mains.append(int(low[-2:]))
            roles[n] = ("main", None)
        else:
            roles[n] = None
    year = None
    if len(mains) == 1:
        year = 2000 + mains[0]
    yy = f"{year % 100:02d}" if year else None
    for n in names:
        if roles[n] is not None:
            continue
        low = n.strip().lower()
        base = n.strip()[:-2] if n.strip()[-2:].isdigit() else n.strip()
        suffix = n.strip()[-2:] if n.strip()[-2:].isdigit() else None
        if base.lower() == C.BREAKDOWN_SHEET_BASE.lower() and suffix:
            roles[n] = ("breakdown", None) if suffix == yy else ("ignored", f"{C.BREAKDOWN_SHEET_BASE} for another year")
        elif base in codes and suffix:
            roles[n] = ("property", base) if suffix == yy else ("ignored", f"{base} sheet for another year ({suffix})")
        elif base.lower() == "main" and suffix:
            roles[n] = ("ignored", "prior-year Main sheet")
        elif suffix and suffix == yy:
            roles[n] = ("candidate", base)
        else:
            roles[n] = ("ignored", "not part of the monthly import")
    return year, roles


# ------------------------------------------------------------------- numbers

def read_amount(sheet, r, c, issues, scope, month=None, what="amount"):
    """(value or None). Text, errors and uncalculated formulas are recorded, never coerced."""
    v, f = sheet.cells.get((r, c), (None, None))
    if v is None:
        if f:
            issues.add("error", "uncalculated", f"{sheet.ref(r, c)} is a formula with no calculated value -- open the workbook in Excel and save it again.", scope, sheet.ref(r, c), month)
        return None
    if _is_number(v):
        return float(v)
    if isinstance(v, str) and v.strip().startswith("#"):
        issues.add("error", "formula_error", f"{sheet.ref(r, c)} shows a spreadsheet error ({v.strip()}).", scope, sheet.ref(r, c), month)
    else:
        issues.add("error", "non_numeric", f"{sheet.ref(r, c)} should be a number ({what}) but is text: {str(v)[:30]!r}.", scope, sheet.ref(r, c), month)
    return None


# ----------------------------------------------------------- month header rows

def month_header(sheet, row, first_col, issues, scope, label_cols_step=2, last_col=40):
    """{month_number: label_col} for the month names in `row` at/after first_col.
    A non-empty header cell that is not a month is a malformed month label."""
    out = {}
    for c in range(first_col, last_col + 1):
        v = sheet.value(row, c)
        if v is None:
            continue
        m = month_number(v) if isinstance(v, str) else None
        if m is None:
            if isinstance(v, str) and (c - first_col) % label_cols_step == 0:
                issues.add("error", "malformed_month", f"{sheet.ref(row, c)} is not a month name: {v.strip()[:20]!r}.", scope, sheet.ref(row, c))
            continue
        if m in out:
            issues.add("error", "duplicate_month", f"Month {v.strip()} appears twice in row {row} of {sheet.name}.", scope, sheet.ref(row, c))
            continue
        out[m] = c
    return out


def _next_marker_row(markers, after, limit):
    later = [r for r in markers if r > after]
    return min(later) if later else limit + 1


def _block_total_row(sheet, label_col, value_col, lo, hi):
    """The row holding a block's total: an unlabelled =SUM(...) cell (else the first unlabelled number)."""
    unlabelled = [r for r in range(lo, hi + 1)
                  if sheet.value(r, label_col) is None and sheet.cells.get((r, value_col), (None, None))[0] is not None]
    for r in unlabelled:
        f = sheet.formula(r, value_col)
        if f and f.upper().startswith("=SUM("):
            return r
    return unlabelled[0] if unlabelled else None


def read_block(sheet, header_row, end_row, months, issues, scope, year):
    """{ym: {"items": [...], "total": float|None, "total_ref": str}} for one block."""
    out = {}
    for m, lc in months.items():
        vc = lc + 1
        key = ym(year, m)
        total_row = _block_total_row(sheet, lc, vc, header_row + 1, end_row)
        stop = total_row if total_row else end_row + 1
        counted = _coeffs(sheet.formula(total_row, vc), vc) if total_row else None   # rows the block's own SUM counts
        items, excluded = [], []
        for r in range(header_row + 1, stop):
            label = sheet.value(r, lc)
            if label is not None and counted is not None and counted.get(r, 0) <= 0 and sheet.cells.get((r, vc)) is not None:
                # a labelled row the block's OWN total does not count: the workbook does not treat it as part of the month
                amount = read_amount(sheet, r, vc, issues, scope, key, what=render_label(label)[:20])
                if amount is not None:
                    excluded.append({"label": render_label(label), "amount": round(amount, 4), "ref": sheet.ref(r, vc)})
                continue
            if label is None:
                # an amount with no label that the block's own total counts is real money: keep it, visibly
                if counted and counted.get(r, 0) > 0 and _is_number(sheet.value(r, vc)):
                    items.append({"label": "(no label)", "amount": round(float(sheet.value(r, vc)), 4), "ref": sheet.ref(r, vc)})
                    issues.add("info", "unlabelled_amount", f"{sheet.ref(r, vc)} has an amount with no label that the block total counts; imported as '(no label)'.", scope, sheet.ref(r, vc), key)
                continue
            amount_cell = sheet.cells.get((r, vc))
            if amount_cell is None:
                continue                                        # a label with no amount is a blank line
            amount = read_amount(sheet, r, vc, issues, scope, key, what=render_label(label)[:20])
            if amount is None:
                continue
            items.append({"label": render_label(label), "amount": round(amount, 4), "ref": sheet.ref(r, vc)})
        total = read_amount(sheet, total_row, vc, issues, scope, key, "block total") if total_row else None
        out[key] = {"items": items, "total": None if total is None else round(total, 4),
                    "total_ref": sheet.ref(total_row, vc) if total_row else None, "excluded": excluded}
    return out


# ------------------------------------------------------------ property sheets

def parse_property_sheet(sheet, code, year, issues):
    scope = code
    prop = {"code": code, "sheet": sheet.name, "title": sheet.text(2, 2), "summary": {}, "opex": {}, "capex": {}, "income": {}}

    # --- summary table, found by its header row
    head = next((r for r in range(1, 30) if sheet.lower(r, 2) == "months"), None)
    if head is None:
        issues.add("error", "no_summary", f"{sheet.name}: the 'Months' summary table was not found.", scope)
        return prop
    cols = {}
    for c in range(3, 16):
        field = SUMMARY_FIELDS.get(sheet.lower(head, c) or "")
        if field:
            cols[field] = c
    missing = [f for f in SUMMARY_FIELDS.values() if f not in cols]
    if missing:
        issues.add("error", "summary_columns", f"{sheet.name}: summary columns missing: {', '.join(missing)}.", scope)
        return prop
    seen = set()
    for r in range(head + 1, head + 16):
        label = sheet.text(r, 2)
        if label is None:
            continue
        if isinstance(label, str) and label.lower().startswith("running"):
            break
        m = month_number(label)
        if m is None:
            issues.add("error", "malformed_month", f"{sheet.ref(r, 2)} is not a month name: {str(label)[:20]!r}.", scope, sheet.ref(r, 2))
            continue
        if m in seen:
            issues.add("error", "duplicate_month", f"Month {label} appears twice in the {sheet.name} summary.", scope, sheet.ref(r, 2))
            continue
        seen.add(m)
        key = ym(year, m)
        row = {}
        for field, c in cols.items():
            row[field] = read_amount(sheet, r, c, issues, scope, key, field) if (r, c) in sheet.cells else None
        row["_formula"] = {f: bool(sheet.formula(r, c)) for f, c in cols.items()}
        prop["summary"][key] = row

    # --- the three detail blocks, found by their labels in column L
    L = 12
    marker_rows = {}
    for name, label in (("opex", "opex"), ("capex", "capex"), ("income", "bookings income")):
        marker_rows[name] = next((r for r in range(1, sheet.max_row + 1) if sheet.lower(r, L) == label), None)
    absent = [n for n, r in marker_rows.items() if r is None]
    if absent:
        issues.add("error", "no_block", f"{sheet.name}: block(s) not found: {', '.join(absent)}.", scope)
        return prop
    all_markers = list(marker_rows.values())
    for name, mr in marker_rows.items():
        header = next((r for r in range(mr, mr + 12) if any(month_number(sheet.value(r, c)) for c in (L,))), None)
        if header is None:
            issues.add("error", "no_block", f"{sheet.name}: month header row under '{name}' not found.", scope)
            continue
        months = month_header(sheet, header, L, issues, scope)
        end = _next_marker_row(all_markers, header, sheet.max_row) - 1
        prop[name] = read_block(sheet, header, end, months, issues, scope, year)
    return prop


# --------------------------------------------------------- expense breakdown

def _coeffs(formula, amount_col):
    """Rows a subtotal formula counts, as {row: coefficient}; None when it is not a plain sum of cells."""
    if not formula or not formula.startswith("="):
        return None
    f = formula[1:].replace(" ", "").upper()
    letter = get_column_letter(amount_col)
    coeffs, consumed = {}, ""
    for m in re.finditer(r"([+-]?)(SUM\(([^()]*)\)|[A-Z]{1,3}\d+|\d+(?:\.\d+)?)", f):
        consumed += m.group(0)
        sign = -1 if m.group(1) == "-" else 1
        body = m.group(2)
        if body.startswith("SUM("):
            parts = m.group(3).split(",")
        elif re.fullmatch(r"\d+(?:\.\d+)?", body):
            if float(body) != 0:
                return None
            continue
        else:
            parts = [body]
        for part in parts:
            ref = re.fullmatch(rf"{letter}(\d+)(?::{letter}(\d+))?", part)
            if not ref:
                return None
            lo = int(ref.group(1))
            hi = int(ref.group(2) or lo)
            for r in range(lo, hi + 1):
                coeffs[r] = coeffs.get(r, 0) + sign
    return coeffs if consumed == f else None


def parse_breakdown(sheet, year, issues, codes=None):
    """{code: {ym: {"items": [...], "opex": float, "capex": float}}} from the "<code> Expenses Breakdown" sections."""
    titles = [(r, C.BREAKDOWN_SECTION_RE.match(str(sheet.value(r, 2)))) for r in range(1, sheet.max_row + 1)
              if isinstance(sheet.value(r, 2), str)]
    titles = [(r, m.group(1)) for r, m in titles if m]
    out = {}
    for i, (t, tag) in enumerate(titles):
        code = next((k for k in (codes if codes is not None else C.PROPERTY_SHEETS) if k.lower() == tag.lower()), None)
        end = (titles[i + 1][0] if i + 1 < len(titles) else sheet.max_row + 1) - 1
        if code is None:
            issues.add("info", "breakdown_unmapped", f"Expense Breakdown section '{tag}' is not a mapped property and was ignored.", None, sheet.ref(t, 2))
            continue
        header = next((r for r in range(t + 1, end + 1) if month_number(sheet.value(r, 2))), None)
        opex_row = next((r for r in range(t + 1, end + 1) if sheet.lower(r, 2) == "opex"), None)
        if header is None or opex_row is None:
            issues.add("error", "breakdown_layout", f"{sheet.name}: section '{tag}' has no month header or OPEX row.", code)
            continue
        capex_row = opex_row + 1
        months = month_header(sheet, header, 2, issues, code, label_cols_step=3)
        sec = {}
        for m, c in months.items():
            key = ym(year, m)
            vc, dc, ac = c, c + 1, c + 2
            opex_f = sheet.formula(opex_row, ac)
            capex_f = sheet.formula(capex_row, ac)
            oc, cc = _coeffs(opex_f, ac), _coeffs(capex_f, ac)
            items, uncounted = [], []
            for r in range(header + 1, opex_row):
                if (r, ac) not in sheet.cells:
                    continue
                amount = read_amount(sheet, r, ac, issues, code, key, "breakdown amount")
                if amount is None:
                    continue
                desc = render_label(sheet.value(r, dc))
                vendor = render_label(sheet.value(r, vc))
                if oc is not None and cc is not None:
                    in_opex, in_capex = oc.get(r, 0) > 0, cc.get(r, 0) > 0
                else:                                                  # subtotals are typed numbers: fall back to the label
                    in_capex = "capex" in desc.lower()
                    in_opex = not in_capex
                item = {"vendor": vendor, "description": desc, "amount": round(amount, 4),
                        "capex": bool(in_capex and not in_opex), "ref": sheet.ref(r, ac)}
                if in_opex or in_capex:
                    items.append(item)
                else:
                    uncounted.append(item)
            sec[key] = {"items": items, "uncounted": uncounted,
                        "opex": read_amount(sheet, opex_row, ac, issues, code, key, "OPEX subtotal"),
                        "capex": read_amount(sheet, capex_row, ac, issues, code, key, "CAPEX subtotal"),
                        "rule": "formula" if (oc is not None and cc is not None) else "label"}
        out[code] = sec
    return out


# --------------------------------------------------------------------- Main Page

def parse_main(sheet, year, issues):
    """Business costs, echo rows, and management-fee control totals, per month."""
    B = 2
    exp_head = next((r for r in range(1, sheet.max_row + 1) if sheet.lower(r, B) == "expenses"), None)
    inc_head = next((r for r in range(1, sheet.max_row + 1) if (sheet.lower(r, B) or "").startswith("gross income")), None)
    sal_row = next((r for r in range(1, sheet.max_row + 1) if sheet.lower(r, B) == "team salaries"), None)
    if exp_head is None or sal_row is None:
        issues.add("error", "main_layout", f"{sheet.name}: the Expenses / Team Salaries blocks were not found.")
        return {"months": {}}
    exp_hdr = next((r for r in range(exp_head, exp_head + 6) if month_number(sheet.value(r, B))), None)
    months = month_header(sheet, exp_hdr, B, issues, None)
    out = {}
    for m, lc in months.items():
        vc, key = lc + 1, ym(year, m)
        gen = next((r for r in range(exp_hdr + 1, sal_row) if sheet.lower(r, lc) == C.MAIN_SUBTOTAL_LABEL), None)
        total_row = _block_total_row(sheet, lc, vc, sal_row + 1, sal_row + 12)
        biz, echoes, salaries = [], {}, []
        stop_items = gen if gen else sal_row
        for r in range(exp_hdr + 1, stop_items):
            label = sheet.value(r, lc)
            if label is None or (r, vc) not in sheet.cells:
                continue
            amount = read_amount(sheet, r, vc, issues, "MAIN", key, render_label(label)[:20])
            if amount is not None:
                biz.append({"label": render_label(label), "amount": round(amount, 4), "ref": sheet.ref(r, vc), "kind": "business"})
        if gen:
            for r in range(gen + 1, sal_row):
                label = sheet.lower(r, lc)
                if label is None or (r, vc) not in sheet.cells:
                    continue
                amount = read_amount(sheet, r, vc, issues, "MAIN", key, label[:20])
                if label in C.MAIN_ECHO_LABELS:
                    echoes[C.MAIN_ECHO_LABELS[label]] = amount
                elif amount:
                    issues.add("review", "main_unknown_row", f"{sheet.ref(r, lc)} '{label}' sits between General and Team Salaries and is not a known property echo; not imported.", "MAIN", sheet.ref(r, vc), key)
        for r in range(sal_row + 1, (total_row or sal_row + 12)):
            label = sheet.value(r, lc)
            if label is None or (r, vc) not in sheet.cells:
                continue
            amount = read_amount(sheet, r, vc, issues, "MAIN", key, render_label(label)[:20])
            if amount is not None:
                salaries.append({"label": render_label(label), "amount": round(amount, 4), "ref": sheet.ref(r, vc), "kind": "salary"})
        out[key] = {"items": biz, "salaries": salaries, "echoes": echoes, "fee_refs": {},
                    "general_total": read_amount(sheet, gen, vc, issues, "MAIN", key, "General") if gen else None,
                    "total": read_amount(sheet, total_row, vc, issues, "MAIN", key, "total") if total_row else None,
                    "fees": {}}
    # management-fee control totals (Gross Income block)
    if inc_head is not None:
        hdr = next((r for r in range(inc_head, inc_head + 6) if month_number(sheet.value(r, B))), None)
        fmonths = month_header(sheet, hdr, B, issues, None) if hdr else {}
        for m, lc in fmonths.items():
            key, vc = ym(year, m), lc + 1
            if key not in out:
                continue
            end = _block_total_row(sheet, lc, vc, hdr + 1, hdr + 40) or hdr + 40
            for r in range(hdr + 1, end):
                code = C.MAIN_FEE_LABELS.get(sheet.label(r, lc) or "")
                if code and (r, vc) in sheet.cells:
                    amount = read_amount(sheet, r, vc, issues, "MAIN", key, "management fee")
                    if amount is not None:
                        out[key]["fees"][code] = round(out[key]["fees"].get(code, 0) + amount, 4)
                        out[key]["fee_refs"].setdefault(code, sheet.ref(r, vc))
    # which section of the Gross Income block each property sits in (R2R = operated, Management SA = managed): model evidence
    sections = {}
    if inc_head is not None and hdr:
        names = {"management long term", "r2r", "management sa", "other"}
        end_row = _block_total_row(sheet, B, B + 1, hdr + 1, hdr + 40) or hdr + 40
        for lc in (fmonths or {}).values():
            current = None
            for r in range(hdr + 1, end_row + 1):
                head = sheet.lower(r, B)
                if head in names and sheet.value(r, B + 1) is None:
                    current = head
                    continue
                label = sheet.label(r, lc)
                if current and label and label not in names:
                    sections.setdefault(current, {}).setdefault(label, False)
                    v = sheet.value(r, lc + 1)
                    if _is_number(v) and v != 0:
                        sections[current][label] = True
    return {"months": out, "sections": sections}


# ------------------------------------------------------------------- top level

def parse_workbook(data, filename, mapping=None):
    """Parse the allowed sheets of a workbook (bytes). `mapping` = sheet codes already known (default: the built-in list).
    Returns plain JSON-safe data."""
    sha = hashlib.sha256(data).hexdigest()
    names = sheet_names(data)
    year, roles = classify(names, mapping)
    issues = Issues()
    result = {"filename": filename, "sha256": sha, "year": year, "roles": {n: list(r) for n, r in roles.items()},
              "properties": {}, "breakdown": {}, "main": {"months": {}}, "issues": issues}
    if year is None:
        issues.add("error", "no_year", "The workbook year could not be detected: expected exactly one sheet named like 'Main Page26'.")
        return result
    to_open = [n for n, (role, _d) in roles.items() if role in ("main", "breakdown", "property", "candidate")]
    all_codes = set(mapping if mapping is not None else C.PROPERTY_SHEETS) | {d for r, d in roles.values() if r == "candidate"}
    assert not any(C.is_blocked(n) for n in to_open)
    try:
        wb_v = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        wb_f = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=False)
    except Exception as exc:                                    # corrupt zip members, bad xml...
        raise WorkbookError(f"The workbook could not be opened: {exc}") from exc

    def load(name, max_row, max_col):
        return Sheet(name,
                     list(wb_v[name].iter_rows(min_row=1, max_row=max_row, max_col=max_col)),
                     list(wb_f[name].iter_rows(min_row=1, max_row=max_row, max_col=max_col)))

    try:
        for name in to_open:
            role, detail = roles[name]
            if role in ("property", "candidate"):
                result["properties"][detail] = parse_property_sheet(load(name, 160, 40), detail, year, issues)
                result["properties"][detail]["candidate"] = role == "candidate"
            elif role == "breakdown":
                result["breakdown"] = parse_breakdown(load(name, 400, 42), year, issues, all_codes)
            elif role == "main":
                result["main"] = parse_main(load(name, 120, 30), year, issues)
    finally:
        wb_v.close()
        wb_f.close()
    return result
