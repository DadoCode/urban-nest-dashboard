# Where each number comes from

Read-only audit of how the dashboard computes its figures (code as of branch
`source-reconciliation`). Nothing here was changed because it "looked suspicious";
items that are semantically uncertain are listed in section 4 for a decision.

## 1. Booking data has exactly one active source per property-month

| Source | What it is | Stored as |
|---|---|---|
| `legacy_aggregate` | The Excel history. One lump of booking income per property-month plus a zero-value "monthly-aggregate" booking row that carries the nights. | `transactions` (`source='excel_import'`, `direction='income'`) and `bookings` (`source='excel_import'`, `reservation_id='monthly-aggregate'`) |
| `detailed` | Real reservations added later: statements, hand entry, synced calendars. | `bookings` (`source` = `upload` / `manual` / `ical`) |

They overlap but cannot be matched line by line, so the dashboard never adds them
together and never lets a reservation replace a month. Per property-month:

1. a row in `booking_source_state` (written **only** by a person, from the
   Reconciliation page) decides; otherwise
2. `legacy_aggregate` if the month has any Excel history; otherwise
3. `detailed` (nothing to protect in a month Excel does not cover).

Importing or confirming a document never writes a row. Status shown to the user:
*Reconciliation needed* (both exist, no decision), *Using detailed bookings*,
*Excel history confirmed*, *Detailed bookings (no Excel for this month)*.

A stay spanning two months contributes each month's share of nights and net
revenue under **that month's** source. "Detailed" is the union of every confirmed
non-Excel reservation, de-duplicated (same code, or same dates and amount when
there is no code; the most recently stored copy wins). The Excel rows are never
modified; reverting restores the exact previous figures.

Every booking-derived KPI goes through five functions in `services/kpis.py`:
`revenue`, `accommodation_revenue`, `booked_nights`, `reservation_count`,
`avg_stay`. Everything else is arithmetic on those.

## 2. Formula audit

"Net" below means after platform fees. Dates are attributed by the month a
transaction is dated in, and by nights falling inside each month for stays.

| Metric | Formula and fields | Managed vs operated | Basis |
|---|---|---|---|
| **Gross Booking Revenue** (tile on property Bookings / managed Overview; `accommodation_revenue`) | Income transactions with `category='booking_income'` dated in range (Excel lumps only in months whose source is Excel) **+** Σ `bookings.net_revenue` × (nights in range ÷ stay nights) for the active source. | Same for both; it is the property's own booking value, not Urban Nest's income. | **Net of platform fees** for uploaded reservations (`net_revenue`). `bookings.gross_revenue` is stored but not used by any KPI. Basis of the Excel lump is whatever the spreadsheet recorded (unverified). |
| **Revenue** (`revenue`) | As above but over **all** income transactions (any category, e.g. historical `reconciliation` adjustments) **+** the same prorated reservation net. | Same. | Net. |
| **Urban Nest Revenue** (`adjusted_revenue`) | Operated: `revenue`. Managed: `business_income` (below). | Differs by model. | Net. |
| **Management Fee Earned** (`business_income`, managed only) | Σ recorded `transactions` with `category='management_fee'`, `direction='expense'` for the property in range; if none recorded, `revenue × management_fee_pct ÷ 100`. | Managed only. Operated flats have no fee. | Recorded fee is whatever was booked; the estimate applies the % to **net** revenue. |
| **Booked nights** (`booked_nights`) | Σ nights of confirmed bookings clipped to the range, from the active source (Excel: the aggregate row's `check_out − check_in`). | Same. | n/a |
| **Reservations / Avg stay** | Count of real (non-aggregate) bookings from the active source; nights ÷ count. | Same. | n/a |
| **Occupancy** | `booked_nights ÷ available_nights`; available = days in range (× number of **currently** active flats for the portfolio). | Same. | n/a |
| **ADR** | `accommodation_revenue ÷ booked_nights`. | Same. | Net per night, despite the name. |
| **RevPAR** | `accommodation_revenue ÷ available_nights`. | Same. | Net. |
| **Property Costs** (Expenses page, `routes/expenses._costs`) | Σ `transactions` with `direction='expense'`, `category != 'management_fee'`, joined to `properties.type='flat'` (Business Costs: `type='overhead'`). | The fee is excluded for managed flats because it is Urban Nest's income. | As recorded (VAT-inclusive for Amazon uploads). |
| **Property Profit** (Overview / Properties; `adjusted_kpi_snapshot.net_profit`) | Operated: `revenue − costs` (raw `kpis.costs`, all expenses). Managed: equals `business_income` (the fee); costs on a managed flat do **not** reduce it. | Differs by model. The Properties page deliberately shows no Property Profit for managed flats. | Net. |
| **Portfolio Operating Profit** (Targets) | `monthly_series → kpi_snapshot` per property: `revenue − costs` where costs include **every** expense (including the management-fee transfer). Not model-aware; overhead is not included. | Managed flats contribute full property revenue less their costs, **not** Urban Nest's share. | Net. |

## 3. What each source field appears to mean

Evidence is from the real exports used in testing; where a statement is an
inference it says so.

### Airbnb transaction history (CSV)
- **Gross earnings** — what the guest paid for the stay including the cleaning fee, before Airbnb's host fee. Inference from `Amount = Gross earnings − Service fee` holding on all 41 reservation rows.
- **Service fee** — Airbnb's host service fee (about 18.6 % of gross in this file).
- **Amount** (Reservation rows) — what the host receives for that reservation = Gross earnings − Service fee.
- **Paid out** (Payout rows only) — the bank transfer. Verified: Σ payout rows = Σ reservation `Amount` + Σ Adjustment / Resolution rows, to the penny (£38,671.42 in the test file).
- **Adjustment / Resolution rows** — later refunds, reversals and damage payouts against a reservation (matched by Confirmation Code); they change what was settled.
- **Date** — the date Airbnb released the money, not the stay date.

Mapping into `bookings`: `gross_revenue = Gross earnings`, `platform_fees = Service fee`, `net_revenue = Amount + adjustments`.

### Booking.com reservations export (XLS)
- **Total payment** — the reservation's total price. Inference: `Commission` is exactly 16.6 % of it on every row.
- **Commission** — Booking.com's commission.
- No payment-service charge column exists, so `Total payment − Commission` is *before* any payment-service charge. Compare the first payout against the statement.
- **Status** — `OK`, `cancelled`, `no_show`; non-OK rows are listed unticked.

Mapping into `bookings`: `gross_revenue = Total payment`, `platform_fees = Commission`, `net_revenue = Total payment − Commission`.

### Canonical definitions
- **Gross booking value**: what the guest pays for the stay (`bookings.gross_revenue`).
- **Net**: gross less platform fees (`bookings.net_revenue`).
- **Payout**: what reaches the bank (net ± adjustments, reconciled against transfer rows where the file has them).

## 4. Semantic uncertainty — needs a decision, nothing changed

1. **"Gross Booking Revenue" is a net figure.** The tile and its tooltip say guest booking value, but the code sums `net_revenue`. Either relabel it, or switch the formula to `gross_revenue`. Changing the formula moves every revenue-derived number.
2. **ADR and RevPAR inherit the same net basis.**
3. **Excel lumps have an unknown basis** (gross, net or payout). Existing-vs-uploaded comparisons are therefore on a net basis and may differ by roughly the platform fee even for a complete month.
4. **Portfolio Operating Profit (Targets) is not model-aware** and includes the management-fee transfer as a cost, so it is not Urban Nest's profit.
5. **Management fee estimate** applies the percentage to net revenue.
6. **Costs on managed flats** appear in Property Costs but do not reduce Urban Nest's Property Profit; if they are not recharged to owners, that cost is invisible.
7. **Months with no Excel history** switch to the detailed bookings automatically. If an upload for such a month is partial, the month will look small until the rest arrives.
8. **Portfolio available nights** uses today's active-flat count for every historical month.
