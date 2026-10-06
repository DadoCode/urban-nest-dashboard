"""What the importer is allowed to read, and what each sheet means.

Nothing here is inferred at import time. A sheet is read only if it is named
below (or is the Main Page / Expense Breakdown sheet for the workbook's year);
everything else is ignored, and a credential-type sheet is never opened at all.
"""
import re

# ---------------------------------------------------------------- never opened
# A sheet whose name contains any of these is excluded BY NAME: not parsed,
# not logged, not stored, not displayed. (The reader never opens it.)
BLOCKED_SHEET_MARKERS = ("login", "password", "credential", "provider")
BLOCKED_SHEET_NAMES = ("Logins & Providers",)


def is_blocked(sheet_name):
    n = sheet_name.replace("&amp;", "&").strip().lower()
    return n in {b.lower() for b in BLOCKED_SHEET_NAMES} or any(m in n for m in BLOCKED_SHEET_MARKERS)


# ---------------------------------------------------------------- sheet roles
# Property sheets are "<code><yy>" (CC26, W826 ...). The code -> canonical
# property id mapping is explicit. The importer uses the canonical id, never the sheet name.
PROPERTY_SHEETS = {
    # code        canonical property id    canonical name
    "CC":        ("crested-court",         "Flat 40, Crested Court, 3 Shearwater Drive, London, NW9 7AD"),
    "LW":        ("lascar-wharf",          "Flat 602, Lascar Wharf Building, 21 Parnham Street, London, E14 7FN"),
    "W8":        ("campbell-hill-w8",      "7A Campden Hill Road, London, W8 7DX"),        # the workbook calls it "7A Campbell Hill"
    "170E":      ("170-miles-building",    "Flat 170, Miles Buildings, Penfold Place, London, NW1 6RP"),
    "175E":      ("175-miles-building",    "Flat 175, Miles Buildings, Penfold Place, London, NW1 6RP"),
    "NW4":       ("nw4",                   "Flat 3, 48 Station Road, NW4 3SX"),           
    "TCR":       ("tottenham-court-road",  "Flats 7 & 8, Shaldon Mansions, 132 Charing Cross Road, London, WC2H 0LA"),
    "11PW":      ("11-perryfield-way",     "Flat 11, Eider Apartments, 73 Perryfield Way, London, NW9 7FD"),
    "22PW":      ("22-perryfield-way",     "Flat 22, Eider Apartments, 73 Perryfield Way, London, NW9 7FD"),      # not yet a dashboard property: created from the workbook when you confirm it
    "19Draycott": ("19-draycott-ave",      "Flat 1, 19 Draycott Avenue, Chelsea, London, SW3 3BS"),
    "S10":       ("44-spooner-road",       "44 Spooner Road"),
}

MAIN_SHEET_BASE = "Main Page"            # -> "Main Page26": business costs + management-fee control totals
BREAKDOWN_SHEET_BASE = "Expense Breakdown"   # -> "Expense Breakdown26": item detail behind the sheets' "Purchases"

# Sheets that are deliberately not imported (matched by lower-cased base name, any year suffix).
IGNORED_SHEETS = {
    "2025 vs 2026": "comparison sheet -- would duplicate transactions",
    "copy of main page": "archive copy of the Main Page -- would duplicate transactions",
    "mcr": "Manchester, held outside the dashboard -- deliberately not imported or offered as a new property",
}

# Breakdown sections are titled "<code> Expenses Breakdown"; the code must be a property code above.
BREAKDOWN_SECTION_RE = re.compile(r"^\s*(\S+)\s+Expenses Breakdown", re.I)

# ------------------------------------------------------------- Main Page rules
# Rows in the Main Page expense block that merely echo a property sheet's costs
# (formulas like =-NW426!F15). They are property costs already imported from the
# property sheet, so they are NOT business costs and are not imported.
MAIN_ECHO_LABELS = {"nw4": "NW4", "lascar wharf": "LW"}
MAIN_SUBTOTAL_LABEL = "general"          # the sum of the itemised rows above it -- never imported as well

# Labels in the Gross Income block -> the property whose management fee they carry (control totals only).
MAIN_FEE_LABELS = {
    "170": "170E", "175": "175E", "w8": "W8", "crested court": "CC", "tottenham": "TCR",
    "11 pw": "11PW", "22 pw": "22PW", "draycott": "19Draycott", "forest gate": "FG",
}

# Managed properties that appear ONLY on the Main Page (no property sheet of their own yet). They can be created and
# updated from what the Main Page actually records (its management fee), and nothing is invented: no income, costs,
# bookings, days or occupancy, and no fee percentage unless the workbook establishes one.
MAIN_ONLY_PROPERTIES = {
    "FG": {"pid": "forest-gate", "name": "29 Station Road, Forest Gate, London, E7 0ES", "model": "managed", "pct": 15.0,
           "aliases": ["Forest gate", "Forest Gate", "29 Station Road", "29 Station Road Forest Gate", "FG"],
           "note": "separate from S10 / 44 Spooner Road (confirmed by you, 6 Oct 2026)"},
}

# ------------------------------------------------------------------ categories
# Label -> dashboard category, using the vocabulary the Excel history already uses.
_CATEGORY_RULES = [
    # a management fee row: "FG Mngmt Fee (15%)", "Management Fee (15%)", but also "Management Faris 15%" (TCR's sheet has no word "fee")
    (r"m(?:a)?n?g?e?m?n?t.*fee|mngm.*fee|management fee|fee.*\(\d+%\)|^\s*(?:fg |faris )?manage(?:ment|r)\b|^\s*mngm", "management_fee"),
    (r"cleaning|\bcleaner\b|\bclean(?:s)?\b", "cleaning"),
    (r"council tax", "council_tax"),
    (r"\b(wifi|wi-fi|broadband|electricity|electric|water|gas|heating)\b", "utilities"),
    (r"\brent\b", "rent"),
    (r"deposit", "deposit"),
    (r"furniture|\btv\b|sofa|mattress", "furniture"),
    (r"plumb|repair|maintenance|boiler", "maintenance"),
    (r"purchases?|amazon|temu", "purchase"),
]
_BUSINESS_RULES = [
    (r"g-?suite|pricelabs|claude|godaddy|domain|website|lodgify|estate ?pilot|\bico\b", "software"),
    (r"\bads?\b|advert|marketing|biz cards|logos|followers", "marketing"),
    (r"insurance", "insurance"),
    (r"companies house|confirmation statement|incorp|accountant", "accounting"),
]


def categorise(label, business=False):
    text = (label or "").strip().lower()
    for pattern, category in (_BUSINESS_RULES if business else []) + _CATEGORY_RULES:
        if business and category == "management_fee":
            continue                    # a management fee is a property-sheet concept; never hide a business cost behind it
        if re.search(pattern, text):
            return category
    return "other"


# --------------------------------------------------------------------- aliases
# For the workbook-PREPARATION step (Claude in chat). The importer receives canonical
# sheet names only; these spellings are what the raw documents and the owner use.
PROPERTY_ALIASES = {
    "CC":        ["CC", "40 Crested Court", "Flat 40 Crested Court", "Crested Court", "Crested Ct"],
    "LW":        ["LW", "602 Lascar Wharf", "Flat 602 Lascar Wharf", "Lascar Wharf", "Lascar"],
    "W8":        ["W8", "7A Campbell Hill", "7A Campden Hill", "Campbell Hill", "Campden Hill", "7A Campden Hill Road"],
    "170E":      ["170E", "170 Miles Building", "Flat 170 Miles Buildings", "170 Miles", "170"],
    "175E":      ["175E", "175 Miles Building", "Flat 175 Miles Buildings", "175 Miles", "175"],
    "NW4":       ["NW4", "Flat 3 NW4", "Flat 3, NW4", "Flat 3", "48 Station Road", "Flat 3 48 Station Road"],
    "TCR":       ["TCR", "Tottenham Court Road", "Tottenham", "Shaldon Mansions", "Flats 7 & 8 Shaldon Mansions"],
    "11PW":      ["11PW", "11 Perryfield Way", "11 PW", "Flat 11", "Eider Apartments Flat 11"],
    "22PW":      ["22PW", "22 Perryfield Way", "22 PW", "Flat 22", "Eider Apartments Flat 22"],
    "19Draycott": ["19Draycott", "19 Draycott Avenue", "19 Draycott Ave", "Draycott Avenue", "Draycott", "Flat 1 19 Draycott Avenue"],
    "S10":       ["S10", "44 Spooner Road", "Spooner Road", "Spooner", "House 44 Spooner Road"],
}

TOLERANCE = 0.01          # money, in pounds
OCCUPANCY_TOLERANCE = 0.005

# ---------------------------------------------------------------- full names
# property id -> (canonical full display name, confidence, where the name comes from, open question or None).
# Names are never invented: where nothing in the workbook, the database or the project's files gives a full name
# or address, the current name stays and the question is raised.
CANONICAL_NAMES = {
    "crested-court":        ("Flat 40, Crested Court, 3 Shearwater Drive, London, NW9 7AD", "high", "confirmed by you, 6 Oct 2026", None),
    "lascar-wharf":         ("Flat 602, Lascar Wharf Building, 21 Parnham Street, London, E14 7FN", "high", "confirmed by you, 6 Oct 2026", None),
    "campbell-hill-w8":     ("7A Campden Hill Road, London, W8 7DX", "high", "confirmed by you, 6 Oct 2026", None),
    "170-miles-building":   ("Flat 170, Miles Buildings, Penfold Place, London, NW1 6RP", "high", "confirmed by you, 6 Oct 2026", None),
    "175-miles-building":   ("Flat 175, Miles Buildings, Penfold Place, London, NW1 6RP", "high", "confirmed by you, 6 Oct 2026", None),
    "nw4":                  ("Flat 3, 48 Station Road, NW4 3SX", "high", "confirmed by you, 6 Oct 2026", None),
    "tottenham-court-road": ("Flats 7 & 8, Shaldon Mansions, 132 Charing Cross Road, London, WC2H 0LA", "high",
                             "confirmed by you, 6 Oct 2026; your website's Shaldon Mansions page puts it 1 minute from Tottenham Court Road", None),
    "11-perryfield-way":    ("Flat 11, Eider Apartments, 73 Perryfield Way, London, NW9 7FD", "high", "confirmed by you, 6 Oct 2026", None),
    "22-perryfield-way":    ("Flat 22, Eider Apartments, 73 Perryfield Way, London, NW9 7FD", "high", "confirmed by you, 6 Oct 2026", None),
    "19-draycott-ave":      ("Flat 1, 19 Draycott Avenue, Chelsea, London, SW3 3BS", "high", "confirmed by you, 6 Oct 2026", None),
    "forest-gate":          ("29 Station Road, Forest Gate, London, E7 0ES", "high", "confirmed by you, 6 Oct 2026; your website's Forest Gate page", None),
    "44-spooner-road":      ("House 44, Spooner Road, Sheffield, S10 5BN", "high", "confirmed by you, 7 Oct 2026", None),
}

# A property that is not in the dashboard yet but whose business model you have already decided: pre-fills the
# NEW PROPERTY panel. It is still only created after you tick it and apply.
NEW_PROPERTY_DEFAULTS = {
    "22-perryfield-way": {"model": "managed", "pct": 15.0, "note": "confirmed by you, 6 Oct 2026: managed at 15%"},
}

# Properties whose income dated BEFORE their start date came from an earlier wrong sync (you confirmed this for NW4). Only rows
# that are exact copies of another property's rows are removed; anything without a twin is left and reported.
PRE_START_DUPLICATE_CLEANUP = {"nw4"}

# The first day a property is part of the portfolio. Earlier periods are NOT ACTIVE / out of scope (not zero, not missing).
START_DATES = {
    "nw4": ("2026-09-01", "taken on from 1 September 2026 (confirmed by you)"),
}

# Main Page business rows you have confirmed are genuine, separate expenses (so they are not flagged as duplicates of a
# property cost): (period, label, amount, why, category).
CONFIRMED_DISTINCT = [
    ("2026-09", "Crescent B. Ads", 3000.0, "Paid to a company to create and run adverts: a genuine business marketing cost, separate from NW4's 3,000 sourcing fee. Confirmed by you, 6 Oct 2026.", "marketing"),
]

# Properties the workbook treats as rent-to-rent (operated): the person confirmed NW4 is rent-to-rent.
MODEL_DECISIONS = {
    # property id: (model, why, fee % when managed)
    "nw4": ("operated", "Dado confirmed Flat 3 NW4 is rent-to-rent; the workbook lists it under R2R and has never recorded a management fee for it", None),
    "forest-gate": ("managed", "Dado confirmed Forest Gate is managed at 15% (7 Oct 2026); the Main Page records explicit fees for June (1,155) and July (210)", 15.0),
}

# Properties that are NOT ACTIVE today (properties.active = 0): excluded from the current portfolio, availability expectations and data-health
# warnings, but every historical row is kept and still shown where it exists. No dates are invented.
STATUS_DECISIONS = {
    "forest-gate": (0, "a one-time management arrangement, not active now (confirmed by you, 7 Oct 2026)"),
    "44-spooner-road": (0, "not active now (confirmed by you, 7 Oct 2026)"),
}
