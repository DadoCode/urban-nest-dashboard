# Monthly workbook: format, workflow and import rules

The dashboard no longer tries to read raw documents to produce the monthly figures.
Each month:

```
raw documents → Claude (chat) prepares / updates the standard workbook → you review it
              → upload that ONE workbook (Documents → Import monthly workbook)
              → the dashboard validates it, previews the change, imports it → (undo if wrong)
```

Claude does the interpretation (Booking.com / Airbnb / Amazon / Temu / cleaning / utilities /
bank / property-name variations). The importer only reads one predictable structure and
refuses anything it cannot place. The existing Source Documents upload stays, as a secondary
reference tool; it no longer drives the monthly figures.

Reference file the format is taken from: `Biz_Accounts_Tracker_2026_Sept_v4.xlsx`.

---

## 1. Security: the credentials sheet

A sheet named `Logins & Providers` (or any sheet whose name contains *login*, *password*,
*credential* or *provider*) is **excluded by name**. The importer never opens it, never reads,
parses, logs, stores or displays it. The reader lists sheet *names* from the zip's
`workbook.xml` and then opens only allow-listed sheets (`services/workbook/config.py`,
`reader.parse_workbook`; asserted in `tests/test_workbook_import.py`).
The uploaded file itself is **not kept**: only the figures read from the allowed sheets are
stored with the import batch.
Please keep credentials out of the monthly workbook anyway; the safest workbook has no such sheet.

## 2. Sheets

For a workbook of year `YYYY`, `yy` = last two digits (2026 → `26`).

| Role | Sheet name | Used for |
|---|---|---|
| **Main page** (required) | `Main Page<yy>` | Business costs; management-fee control totals |
| **Expense breakdown** | `Expense Breakdown<yy>` | Itemised purchases behind each property sheet's "Purchases" line |
| **Property sheets** | `<code><yy>`, see §3 | Income, Opex, Capex, Days Booked, control totals |
| Ignored (named) | `2025 vs 2026`, `Copy of Main Page<yy>`, `MCR<yy>` | Comparison / archive / not a dashboard property |
| Ignored (other year) | any `<code>` or `Main`/`Expense Breakdown` sheet with a different `yy` | Prior-year history |
| **Blocked** | `Logins & Providers` | Never opened |
| **Unmapped** | `<anything><yy>` that is not in §3 | Stops import for that sheet: *"New property detected — map this sheet before importing."* |

The year is the `yy` of the single `Main Page<yy>` sheet. No such sheet (or two) → nothing imports.
Duplicate sheet names → nothing imports.

## 3. Canonical property list and aliases

The sheet → property mapping is explicit (`PROPERTY_SHEETS` in `services/workbook/config.py`),
never inferred. The importer writes by **canonical property id**, not by sheet name.

| Sheet code (`+yy`) | Canonical property id | Canonical name | Aliases Claude should map to this sheet |
|---|---|---|---|
| `CC` | `crested-court` | 40 Crested Court | CC, 40 Crested Court, Crested Court, Crested Ct |
| `LW` | `lascar-wharf` | 602 Lascar Wharf | LW, 602 Lascar Wharf, Lascar Wharf, Lascar |
| `W8` | `campbell-hill-w8` | 7A Campden Hill | W8, 7A Campbell Hill, 7A Campden Hill, Campbell Hill, Campden Hill |
| `170E` | `170-miles-building` | 170 Miles Building | 170E, 170 Miles Building, 170 Miles, 170 |
| `175E` | `175-miles-building` | 175 Miles Building | 175E, 175 Miles Building, 175 Miles, 175 |
| `NW4` | `nw4` | Flat 3 NW4 | NW4, Flat 3 NW4, Flat 3, NW4 |
| `TCR` | `tottenham-court-road` | Tottenham Court Road | TCR, Tottenham Court Road, Tottenham |
| `11PW` | `11-perryfield-way` | 11 Perryfield Way | 11PW, 11 Perryfield Way, 11 PW |
| `22PW` | `22-perryfield-way` | 22 Perryfield Way | 22PW, 22 Perryfield Way, 22 PW (created from the workbook on first import, after you confirm it) |
| `19Draycott` | `19-draycott-ave` | 19 Draycott Avenue | 19Draycott, 19 Draycott Avenue, 19 Draycott Ave, Draycott Avenue, Draycott |
| `S10` | `44-spooner-road` | 44 Spooner Road | S10, 44 Spooner Road, Spooner Road, Spooner |

The aliases live in the **workbook-preparation step** (Claude, in chat); the importer receives canonical
sheets. Identity is also stored permanently in the database (`workbook_sheet_map`, `property_identity_aliases`),
so a renamed property keeps working: the property **id never changes**, the old name stays as an alias.
Matching order, never fuzzy: exact id → exact alias → normalised alias → sheet code → a person confirms.
`MCR<yy>` (Manchester) is ignored on purpose and is never offered as a new property.

### New properties

A `<code><yy>` sheet that is not mapped is a **new property candidate**. If its code or title matches an existing
alias it is that property (no duplicate is created). Otherwise the preview shows **NEW PROPERTY DETECTED** with the
proposed name, id, sheet, aliases and the model the workbook suggests (R2R section = operated; Management SA section
or a fee row = managed, with the % read from the fee row/label or the Main Page fee ÷ income), and nothing is created
until you tick it, confirm the name and the model (and the fee % if managed) and apply. The import then creates the
property (stable id, aliases, sheet mapping, defaults) and imports its rows. Re-importing never creates a second copy;
undo removes it again only while nothing else has been recorded against it.

## 4. Property sheet layout (`<code><yy>`)

Blocks are found by **label**, never by row number (the same block sits on different rows on
different sheets). Anything outside these areas is ignored.

**Title** `B2` (informational).

**Summary table.** Header row has `Months` in column B and the column titles
`Net Profit`, `Operating Profit`, `Income`, `Total Costs`, `Opex`, `Capex`, `Occupancy`, `Days Booked`
(any order). Below it, one row per month, column B = full month name. A `Running P/L` row ends the table.
These are **control totals** (§7). Only `Days Booked` is also imported.

**Detail blocks** (column `L` onwards, one *label | amount* column pair per month, 2 columns wide,
month names as the header row):

| Block | Marker in column L | Meaning | Becomes |
|---|---|---|---|
| Opex | `OPEX` (header row of months below) | Operating costs for the month | `transactions` expense, `capex=0` |
| Capex | `CAPEX` | Capital costs | `transactions` expense, `capex=1` |
| Bookings Income | `Bookings Income` | One row per stay / extra / refund | `transactions` income, `booking_income` |

Each block ends in an unlabelled `=SUM(...)` total. Rules:
- A **label with an amount** is a row. A label with no amount is a blank line.
- An amount with **no label that the block's own `SUM` counts** is imported as `(no label)` and reported.
- **Negative amounts are legitimate** (refunds, guest deductions, credits) and are imported as negative rows.
- Text in an amount cell, spreadsheet errors (`#REF!`…), or a formula with no calculated value are errors.
- A stay label such as `4-9` that Excel turned into a date is rendered back as `m-d`. The label is descriptive only; stay dates are not used.
- A **management fee row** is any label matching *mngmt/management … fee* → category `management_fee`.
- Rows are dated the **1st of the month** (as the history is).

**Days Booked** is the authoritative operating figure. It becomes one `monthly-aggregate`
booking per month (nights = Days Booked), exactly the form the history uses. Reservation-level
rows are not needed. Days Booked blank but Occupancy present → derived and noted.

## 5. Expense Breakdown sheet

Sections titled `<code> Expenses Breakdown` (only `CC`, `LW`, `W8` today). Each month is a
3-column group *vendor | description | amount* under month-name headers, ending in `OPEX` and
`CAPEX` subtotal rows. **The workbook's own subtotal formulas decide Opex vs Capex**: a row counts as
Capex if the `CAPEX` formula's cell list/range contains it, Opex if the `OPEX` formula does. (If a
subtotal is a typed number, a `(capex)` label is used instead.) When a property has breakdown rows
for a month, they **replace** that property sheet's `Purchases` line (which is only a link to the
subtotal); the two are reconciled (`Purchases detail vs sheet lump`). Positive values here are expenses
(the generic "positive = Booking Income" rule does **not** apply).

## 6. Main Page (`Main Page<yy>`)

- **Expenses block** (month column pairs from column B): itemised company rows → **Business costs**
  (the single `general-overheads` cost centre; never a fake property).
  - The `General` row is the **sum of the itemised rows above it**: never imported on top of them.
  - `NW4` and `Lascar Wharf` rows are **echoes** of the property sheets' total costs
    (`=-NW4<yy>!F…`). They are property costs already imported from those sheets, so they are *not* business costs.
  - `Team Salaries` rows → category `salary` (the history already books `Dado salary` this way).
    The preview notes that owner pay is included.
- **Gross Income block**: the *management* section lists each managed property's monthly fee.
  Used as a **control total** and, where a property sheet has no fee row, as the recorded fee
  (`Management fee (Main Page)`). Long-term management, R2R and "Other" income lines are not imported.

## 7. Field mapping: workbook field → dashboard field → definition

| Workbook field | Dashboard field | Definition / note |
|---|---|---|
| Bookings Income rows | `transactions` income `booking_income` | Booking value the property took. *Gross Booking Revenue* tile (net of platform fees; see `data-sources-and-kpi-audit.md` §4.1). |
| Opex rows | `transactions` expense, `capex=0` | Includes the management-fee row on sheets that carry one. Property Costs (Expenses page) excludes the fee row by rule. |
| Capex rows | `transactions` expense, `capex=1` | Capital items (sourcing fee, furniture…). |
| Management fee row / Main Page fee | `transactions` expense `management_fee` | **Management Fee Earned** (managed). Else the dashboard estimates `% × income`. |
| Days Booked | `bookings` `monthly-aggregate` (nights) | Booked nights → Occupancy = nights ÷ days in month. |
| Income (summary) | control only | Must equal Σ income rows. |
| Opex / Capex (summary) | control only | Must equal Σ Opex / Σ Capex rows. |
| Total Costs (summary) | control only | `−(Opex + Capex)`. |
| Net Profit (summary) | control only | `Income + Total Costs`. **Not** imported as Property Profit; for managed flats the dashboard's Property Profit is the fee. |
| Operating Profit (summary) | control only | `Income − Opex`. |
| Occupancy (summary) | control only | Days Booked ÷ days in the month; a different divisor is flagged. |
| Main Page `General`, total, echo rows | control only | Validate the business-cost import. |

Workbook formulas (audited from the reference file): `Net Profit = Income + Total Costs`,
`Operating Profit = Income − Opex`, `Total Costs = −(Opex + Capex)`, `Income/Opex/Capex = SUM of their block`,
`Occupancy = Days Booked ÷ 30 (or 31…)`. **Days Booked is the only hand-keyed summary field.**

Managed vs operated is the **dashboard's** property model (`properties.management_fee_pct`), never
the workbook's: operated properties show *Urban Nest Revenue / Property Costs / Property Profit*; managed
properties show *Gross Booking Revenue / Management Fee Earned*. If the workbook and the property's model
disagree the import is **flagged** (see *Management model*, below); nothing is silently changed.

## 8. Validation (before anything is written)

Blocking for the affected property (others still import): unmapped sheet; mapped property missing from the
dashboard; malformed or duplicate month label; text / error / uncalculated amounts; occupancy < 0% or > 100%;
negative, fractional or too-many Days Booked; missing Opex/Capex/Income block or summary table.
Blocking for everything: no single `Main Page<yy>`; duplicate sheet names; not an `.xlsx`.
Sheets missing from the workbook are *not* errors: that property is left exactly as it is.
Problems in other months do not block the month being imported.

## 9. Reconciliation (per property and month; PASS / REVIEW)

Workbook value vs what would be imported, tolerance £0.01 (occupancy 0.5 pts):
Income, Opex, Capex, Total costs, Net profit (control), Operating profit (control), Days booked, Occupancy,
*Detail rows vs block totals*, *Purchases detail vs sheet lump*, *Breakdown subtotals vs rows*,
*Management fee* (Main Page vs fee row) or *Management fee rate* (Main Page fee ÷ income vs the
property's %), *Management model* (dashboard says managed, workbook records no fee),
*Income without booked nights*. A blank control total with detail present is **REVIEW**.
Business costs: `General` = Σ items, Main Page total = items + echoes + salaries, echo rows = property sheets' costs.

Anything REVIEW is **unticked by default** in the preview and needs an explicit "I have reviewed the flagged items".

## 10. Import behaviour

- **One month at a time** ("Import September 2026 only"). The default is the latest month where at least half
  the properties have income. Each month tab shows how many rows differ from the dashboard.
- **Preview before anything is written**: per property, *current dashboard → after import → change* for revenue,
  property costs, management fee, days booked, occupancy and profit, computed with the real KPI code on a
  rolled-back copy; row-level NEW / CHANGED / REMOVED / UNCHANGED; the reconciliation table.
- **Re-import is deterministic.** Rows are matched (direction, Opex/Capex, description, amount); matching rows are
  never rewritten, so the same workbook twice changes nothing ("Nothing to import"). A corrected value changes exactly
  that row. A stale preview (ledger moved since it was shown) is refused.
- **Only the aggregate layer is replaced** (rows from earlier workbook / Excel imports for that property-month).
  Uploaded documents and hand-entered rows are never touched; their costs add to the workbook's.
- **Booking source**: the workbook's monthly figures are the active source for each imported property-month
  (recorded in `booking_source_state`), unless a person already chose "detailed". Detailed reservations are for drill-down
  and never silently replace workbook totals.
- **Batches** (`import_batches`): filename, upload time, period, workbook hash, properties, row count, before / after
  totals, reconciliation, status (staged / applied / undone / cancelled). Every written row carries `import_batch_id` and its source cell.
- **Undo** restores the previous state exactly (rows added are removed, rows replaced are re-inserted with their original ids).
  It is refused if a later import touched the same property-month or if an imported row was edited since.
- **Provenance**: a quiet "Updated from workbook · 5 Oct 2026" under the context bar for periods an import has written; the
  Expenses ledger shows `#<batch>` per row and can be filtered to one import (`t_batch`).

## 11. Claude's monthly workflow

1. Read the new raw documents (Booking.com / Airbnb statements, Amazon / Temu orders, cleaning, utilities, bank, misc).
2. Open last month's workbook and **keep the layout exactly**: same sheet names, blocks and month columns.
3. Map every document line to a canonical property using §3 (aliases), never inventing a new sheet name.
4. Add booking income rows, Opex and Capex rows to the right property sheet and month column; add itemised purchases to the Expense Breakdown (CC / LW / W8).
5. Add business costs and salaries to `Main Page<yy>` (itemised; do not type over `General`).
6. Enter **Days Booked** for each property (the only hand-keyed figure).
7. Recalculate the workbook (Excel / LibreOffice) so every formula has a value, then check the summary rows add up.
8. Flag anything ambiguous (unknown property, unreadable line, refund) to you in chat instead of guessing.
9. You review the workbook, then upload it: Documents → Import monthly workbook.
10. Check the preview for the month, resolve any REVIEW items (usually a workbook fix), apply, and read the "workbook = ledger = dashboard" panel.

## 12. Known workbook quirks the importer reports (from the Sept v4 file)

- 170E Jan/Jun, 175E Jun: block totals count an amount that has no label (imported as `(no label)`).
- 19Draycott: summary rows blank; its Opex block lists purchases as negatives and puts the fee **below** the block total (fee excluded from its own total).
- CC Jan: Main Page fee (346.18) ≠ the property sheet's fee (393.46).
- TCR Apr–Jun: Main Page fee ≠ 15% of income.
- 11PW Jun: occupancy uses a different month length than Days Booked.
- LW Jan: the `Purchases` link (−149.89) differs from the breakdown subtotal (160.89).
- W8 Jan: a breakdown amount typed as text (`£1.59`): blocks that property-month.
- NW4: the Main Page lists it under R2R while the dashboard models it as managed at 15%: flagged as *Management model* every month it has income.
- Main Page echo rows (NW4, Lascar Wharf) are **not** imported. The earlier history booked a `Lascar Wharf` rent row under business costs; that is the same money as the LW sheet's costs.

## 13. Not built (by decision)

- No silent creation of properties: a new property is only ever created from a confirmed preview.
- No import of management-company income, R2R income lines, "Other" income, or prior-year sheets.
- No attempt to interpret raw documents inside the dashboard.

## 14. Names, model corrections and the clean-up batch

- **Full names** are the primary label everywhere (`properties.name` and `address` both hold the canonical full address; the
  stable id never changes; short codes and every previous name stay as aliases). The ten names you confirmed are in
  `config.CANONICAL_NAMES`. **44 Spooner Road (S10)** has no full address yet, and **29 Station Road, Forest Gate** is not
  matched to any property: the Main Page lists "Forest gate" (managed) and S10 (rent-to-rent) as two different properties, so
  it is neither created nor guessed.
- **Start date.** `properties.start_date` (an existing, previously unused column) is the first day a property is part of
  the portfolio. Before it a property is NOT ACTIVE / out of scope: available nights are 0 (property and portfolio
  occupancy), data health says "Not active until <date>" and lists nothing as missing, and the workbook import marks the
  month "Not active yet" (no import, no flags, nothing removed; the workbook's own pre-start rows are reported, not
  imported). Historical rows are never rewritten. NW4 starts 2026-09-01.
- **Models** follow the workbook's evidence. NW4 is rent-to-rent (confirmed): operated, no management fee. 22 Perryfield Way is
  managed at 15% (confirmed) and is created from the workbook once you tick it in the preview.
  The importer flags any property whose dashboard model disagrees with its fee rows (*Management model*), whose fee is not
  its % of income (*Management fee rate*), whose Main Page fee differs from its sheet's, or that has no workbook control.
- **Echo / duplicate rules.** A business row whose description names a property and equals that property's total costs for the
  month to the penny is a *confirmed echo* (eight "Lascar Wharf" rows, Jan–Aug 2026). The cleanup removes only those. A
  Main Page business row ≥ £50 that equals a property row of the same month to the penny is a *possible duplicate*: flagged in
  the preview with **Leave out** / **Separate expense** boxes; either choice is remembered (by month, label and amount).
  "Crescent B. Ads" £3,000 (Sept) is recorded as a genuine marketing cost, separate from NW4's £3,000 sourcing fee.
- **Draycott.** September's purchases are formulas `=August × −1` (a reversal), the typed fee (241.94) is 23.5% of income
  against a 12% setting, the Opex block total excludes the fee row below it, and Days Booked is blank: it stays REVIEW / NO
  CONTROL until the workbook is understood. Nothing is corrected silently.
- **Clean-up** (`scripts/apply_property_cleanup.py --db PATH [--apply]`): dry-run by default; `--apply` takes a timestamped backup
  first, prints the SHA-256, verifies the backup, runs the additive migration and applies the clean-up as one undoable batch.

## 15. Main-Page-only properties, pre-opening costs, and the workbook's own totals

- **Main-Page-only managed properties** (`config.MAIN_ONLY_PROPERTIES`; today **29 Station Road, Forest Gate, London, E7 0ES**, alias
  "Forest gate"): a property does not need its own sheet to exist. Its row in the Main Page *Management SA* block is the only thing
  imported: the recorded management fee for each month, as a `management_fee` expense (Forest Gate: June 1,155 at `Main Page26!M47`,
  July 210 at `O47`). No income, costs, bookings, days or occupancy are invented, and no fee percentage unless the workbook
  establishes one. It is created, after you tick it in the preview, as **managed with no percentage**: `properties.is_managed = 1`,
  `management_fee_pct` empty. Management Fee Earned is then the recorded fee only (never an estimate), and the missing percentage
  is flagged as configuration still needed. It is separate from S10 / 44 Spooner Road.
- **`is_managed`** is the explicit managed flag (backfilled from the existing percentages); `management_fee_pct` may be empty for a
  managed property whose percentage is not known. Every managed/operated decision goes through `services.common.is_managed`.
- **Pre-opening costs.** Before a property's `start_date`, a month that has cost rows imports **costs only** (labelled PRE-OPENING):
  no income, no booking aggregate, no booking-source decision, and the property stays NOT ACTIVE (0 available nights, "Not active"
  in data health and on the Properties page). A month with no costs is "Not active yet" and imports nothing. NW4: August has
  six purchases totalling 78.71.
- **The workbook's own totals decide what a block contains.** A labelled row that the block's `=SUM(...)` does not count is reported
  ("Rows the workbook's own total does not count") and **not imported**: NW4 August's `lenor crease` 0.30 and `sponge eraser` 0.7475,
  Draycott August's `memory foam topper taken to w8` and Draycott September's fee row (the fee still comes from the Main Page).
- **Wrongly attached history.** Income dated before a property's start date is removed only when it is an exact copy (date, description,
  amount) of another property's row and the property is listed in `PRE_START_DUPLICATE_CLEANUP` (NW4: 17 rows, July 5,284.27 and
  August 3,483.97, all copies of 175 Miles Building's income). Rows with no twin are left and reported. Undoable.


## 16. Active, not started, inactive; how the Properties count is calculated

Three different things are kept apart:

| | Meaning | Where it lives | Effect |
|---|---|---|---|
| A. Not started | `start_date` is after the period | `properties.start_date` | "Not active until <date>": no availability, no operating KPIs; pre-opening costs may still be imported (NW4 August) |
| B. Active now | `active = 1` | `properties.active` | always expected to produce data; counted in the header |
| C. Inactive | `active = 0` (optionally `end_date`) | `properties.active`, `properties.end_date` | "Inactive": never "missing documents"; kept in history, and counted for availability only in months with recorded activity (Forest Gate Jun/Jul, Spooner history) |

`end_date` is optional and is never invented.

**Properties count** (header on the Properties page): active flats only. In a historical period it adds "+ N inactive with activity in this period". The business cost centre ("Portfolio General Expenses") is never a property. Current records: 13 = 12 flats (10 active, 2 inactive: Forest Gate, Spooner) + 1 business cost centre.

**Final configuration (Sept v4 workbook):** Forest Gate = 15% managed, inactive, explicit fee rows kept (June £1,155, July £210), no estimates. Spooner = "House 44, Spooner Road, Sheffield, S10 5BN", operated, inactive, history preserved. NW4 starts 2026-09-01.

**Fee-label rule:** a row whose label is a management fee (including "Management Faris 15%", "FG Mngmt Fee (12%)", "Mngmnt Fee") on a property sheet is category `management_fee`; business-cost rows never become management fees. The row diff is category-aware: a row whose category changes shows as CHANGED, not UNCHANGED.

**Calculated-blank formulas:** a formula cell whose cached result is an empty string is blank by design, not an "uncalculated" error.

## 17. Drilldowns: how a number leads to its records

Rule: the number you click equals the breakdown you land on. Nothing is recalculated for a drilldown; every page reuses the KPI functions.

| Figure | Opens | Reconciles to |
|---|---|---|
| Overview Urban Nest Revenue / Property Profit | Properties (summary line: operated + management fees) | `adjusted_kpi_snapshot` for the portfolio |
| Overview "Operated Property Costs" bar | Expenses `scope=property&model=operated` | operated-property costs, to the penny |
| Overview Property Costs / Business Costs line | Expenses `scope=property` / `scope=business` | the Expenses headline totals |
| Occupancy / RevPAR | Bookings → Performance (rows + Portfolio line), then the property's Booked nights | booked ÷ available nights |
| A property's revenue, fee, occupancy | Bookings tab: Revenue records, Management fee, Booked nights | `accommodation_revenue`, `revenue`, `business_income`, `booked_nights` |
| Targets row | the property's Performance for that month; the drawer shows gross revenue − costs = Operating Profit | `monthly_series` |

Source: a row written by a monthly workbook import shows "Imported from <file> · Batch #N · <sheet!cell> · applied <time>" and links to `/imports/N#prop-<property>`, which opens that property's reconciliation block. Such rows are read-only one at a time (edit or delete is refused server-side); correct the workbook and re-import, or undo the import. Rows from the earlier Excel history say "Earlier Excel import": no batch or cell was recorded for them. A workbook month holds booked nights and income as totals (no individual reservations), and the pages say so.

REVIEW pills come from the reconciliation stored on the import batch; the fee-rate check is re-judged against the property's current configured percentage.
