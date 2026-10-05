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
| `22PW` | `22-perryfield-way` | 22 Perryfield Way | 22PW, 22 Perryfield Way, 22 PW (**not yet a dashboard property**: its import is blocked until it is added) |
| `19Draycott` | `19-draycott-ave` | 19 Draycott Ave | 19Draycott, 19 Draycott Ave, Draycott |
| `S10` | `44-spooner-road` | 44 Spooner Road | S10, 44 Spooner Road, Spooner Road, Spooner |

The aliases live in the **workbook-preparation step** (Claude, in chat). The importer receives
canonical sheets only. A new property is added by (1) creating it in the dashboard, then
(2) adding its row to `PROPERTY_SHEETS` (and aliases); there is deliberately no auto-create.

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

- No auto-creation of properties or sheet mappings.
- No import of management-company income, R2R income lines, "Other" income, or prior-year sheets.
- No attempt to interpret raw documents inside the dashboard.
