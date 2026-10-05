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
    "CC":        ("crested-court",         "40 Crested Court"),
    "LW":        ("lascar-wharf",          "602 Lascar Wharf"),
    "W8":        ("campbell-hill-w8",      "7A Campden Hill"),        # the workbook calls it "7A Campbell Hill"
    "170E":      ("170-miles-building",    "170 Miles Building"),
    "175E":      ("175-miles-building",    "175 Miles Building"),
    "NW4":       ("nw4",                   "Flat 3 NW4"),             # full address not found anywhere in the project: left as is, flagged
    "TCR":       ("tottenham-court-road",  "Tottenham Court Road"),
    "11PW":      ("11-perryfield-way",     "11 Perryfield Way"),
    "22PW":      ("22-perryfield-way",     "22 Perryfield Way"),      # mapped, but not (yet) a dashboard property
    "19Draycott": ("19-draycott-ave",      "19 Draycott Avenue"),
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
    "11 pw": "11PW", "22 pw": "22PW", "draycott": "19Draycott",
}

# ------------------------------------------------------------------ categories
# Label -> dashboard category, using the vocabulary the Excel history already uses.
_CATEGORY_RULES = [
    (r"m(?:a)?n?g?e?m?n?t.*fee|mngm.*fee|management fee|fee.*\(\d+%\)", "management_fee"),
    (r"cleaning", "cleaning"),
    (r"council tax", "council_tax"),
    (r"\b(wifi|wi-fi|broadband|electricity|electric|water|gas|heating)\b", "utilities"),
    (r"\brent\b", "rent"),
    (r"deposit", "deposit"),
    (r"furniture|\btv\b|sofa|mattress", "furniture"),
    (r"plumber|repair|maintenance|boiler|aircon", "maintenance"),
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
        if re.search(pattern, text):
            return category
    return "other"


# --------------------------------------------------------------------- aliases
# For the workbook-PREPARATION step (Claude in chat). The importer receives canonical
# sheet names only; these spellings are what the raw documents and the owner use.
PROPERTY_ALIASES = {
    "CC":        ["CC", "40 Crested Court", "Crested Court", "Crested Ct"],
    "LW":        ["LW", "602 Lascar Wharf", "Lascar Wharf", "Lascar"],
    "W8":        ["W8", "7A Campbell Hill", "7A Campden Hill", "Campbell Hill", "Campden Hill"],
    "170E":      ["170E", "170 Miles Building", "170 Miles", "170"],
    "175E":      ["175E", "175 Miles Building", "175 Miles", "175"],
    "NW4":       ["NW4", "Flat 3 NW4", "Flat 3, NW4"],
    "TCR":       ["TCR", "Tottenham Court Road", "Tottenham"],
    "11PW":      ["11PW", "11 Perryfield Way", "11 PW"],
    "22PW":      ["22PW", "22 Perryfield Way", "22 PW"],
    "19Draycott": ["19Draycott", "19 Draycott Avenue", "19 Draycott Ave", "Draycott Avenue", "Draycott"],
    "S10":       ["S10", "44 Spooner Road", "Spooner Road", "Spooner"],
}

TOLERANCE = 0.01          # money, in pounds
OCCUPANCY_TOLERANCE = 0.005

# ---------------------------------------------------------------- full names
# property id -> (canonical full display name, confidence, where the name comes from, open question or None).
# Names are never invented: where nothing in the workbook, the database or the project's files gives a full name
# or address, the current name stays and the question is raised.
CANONICAL_NAMES = {
    "crested-court":        ("40 Crested Court",       "high",   "dashboard name + workbook sheet title", None),
    "lascar-wharf":         ("602 Lascar Wharf",       "high",   "dashboard name + workbook sheet title", None),
    "campbell-hill-w8":     ("7A Campden Hill",        "high",   "dashboard name + your property list (the workbook sheet says '7A Campbell Hill')", None),
    "170-miles-building":   ("170 Miles Building",     "high",   "dashboard name + workbook sheet title", None),
    "175-miles-building":   ("175 Miles Building",     "high",   "dashboard name + workbook sheet title", None),
    "nw4":                  ("Flat 3 NW4",             "low",    "dashboard name + workbook sheet title only",
                             "NW4 is a postcode district: no street address exists in the workbook, the database, the documents or the project files. Needs the address from you."),
    "tottenham-court-road": ("Tottenham Court Road",   "medium", "dashboard name + workbook sheet title",
                             "No street number appears anywhere; kept as is."),
    "11-perryfield-way":    ("11 Perryfield Way",      "high",   "dashboard name + workbook sheet title", None),
    "22-perryfield-way":    ("22 Perryfield Way",      "high",   "workbook sheet title + your property list", None),
    "19-draycott-ave":      ("19 Draycott Avenue",     "high",   "your property list ('19 Draycott Avenue'); the workbook and dashboard say 'Ave'", None),
    "44-spooner-road":      ("44 Spooner Road",        "high",   "dashboard name + workbook sheet title", None),
}

# Properties the workbook treats as rent-to-rent (operated): the person confirmed NW4 is rent-to-rent.
MODEL_DECISIONS = {"nw4": ("operated", "Dado confirmed Flat 3 NW4 is rent-to-rent; the workbook lists it under R2R and has never recorded a management fee for it")}
