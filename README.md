# Urban Nest Estates — Business Dashboard

A local web app for real portfolio accounting: a homepage (portfolio
revenue/profit/occupancy, goals, year-on-year) and a page per flat with its
own goal, charts, itemized expenses, booking-calendar sync, and document
uploads. Backed by `data/dashboard.db` (SQLite) — no PriceLabs or other live
API involved.

## Run it

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python3 dashboard/app.py        # http://localhost:5050
```

There's no always-on server — this only runs while that command is running.
Leave the terminal tab open for as long as you want the dashboard reachable;
`Ctrl+C` stops it. Optional: copy `.env.example` to `.env` and set
`ANTHROPIC_API_KEY` to turn on AI extraction for uploaded documents (see
below) — without it, uploads still work, you just fill in the line items by
hand afterwards instead of having them pulled out automatically.

## Files

- `dashboard/app.py` — the Flask app: routes for the homepage, each flat's
  page, adding an apartment, goals, manual expenses, document upload/review,
  and calendar sync.
- `dashboard/db.py` — SQLite schema and connection helper.
- `dashboard/extraction.py` — Claude-based line-item extraction from an
  uploaded document (Amazon/Temu order, cleaning invoice, booking or bank
  statement, receipt). Returns `None` with no `ANTHROPIC_API_KEY` set, so the
  app falls back to a blank manual-entry form instead of crashing.
- `dashboard/ical_sync.py` — fetches and parses a listing's booking-calendar
  export URL (the .ics file every major platform provides) into nights
  booked per month. Dependency-free: the .ics format used by these exports
  is simple enough to parse with the standard library.
- `dashboard/templates/`, `dashboard/static/style.css` — the pages.
- `scripts/import_excel_tracker.py` — one-time/repeatable import from the
  real accounts tracker (an .xlsx with one sheet per flat per year) into
  `data/dashboard.db`. Safe to re-run after the workbook changes: it only
  replaces rows tagged `source='excel_import'`, never anything added later
  by hand, by a document upload, or by a calendar sync. See the script's
  docstring for the sheet layout it expects.

## The database

`data/dashboard.db` — SQLite, five tables:

- `properties` — a flat, or the `general-overheads` pseudo-property for
  shared/non-flat-specific costs (flagged `is_overhead`, kept out of the
  homepage's per-flat grid). Also holds each flat's `ical_url` /
  `ical_synced_at` for calendar sync.
- `monthly_summary` — income/costs/profit/occupancy per flat per month,
  tagged by `source`: `excel_import` (the trusted historical ledger, never
  overwritten once it holds a real figure) / `derived` (recomputed from
  `expense_items` whenever a manual entry or confirmed upload touches that
  month) / `ical` (occupancy filled in from a calendar sync).
- `goals` — revenue/profit targets per flat per month.
- `expense_items` — itemized line items, tagged by `category`
  (`booking_income` / `opex` / `capex` / `purchase` / `cleaning` /
  `utilities` / `overhead` / `other`) and by `source` (`excel_import` /
  `manual` / `upload`). Amounts are always a plain positive number —
  `category = 'booking_income'` is what makes something income rather than
  a cost, not the sign.
- `documents` — uploaded files and whatever got extracted from them.

## The ongoing monthly workflow

This is the point of the upload feature: once a month, upload whatever came
in for a flat (Amazon/Temu orders, cleaning invoices, a booking-platform
payout statement, a bank statement, receipts — PDF or photo) on that flat's
page. With `ANTHROPIC_API_KEY` set, Claude pulls out line items for you to
review and edit before anything lands in the ledger; without a key, you get
a blank form to fill in by hand instead. Nothing is written to
`expense_items` until that review is confirmed — and confirming
automatically recomputes that flat's income/costs/profit for the month the
items fall in, so a flat's numbers can be built entirely from uploads/manual
entries from here on, with no need to touch the original spreadsheet again.

Separately, each flat's page has a **booking calendar sync**: paste its
Airbnb "Export calendar" link (or Booking.com/Vrbo's sync-calendars URL) and
hit Sync to fill in occupancy automatically from actual reservations, no
typing required. Re-run it any time — it remembers the URL.

The one rule threading through all of this: a month that already has a
*real* (non-placeholder) `excel_import` figure is never overwritten by a
derived recompute or a calendar sync. That historical ledger stays
authoritative; uploads and calendar syncs only fill in what it doesn't cover.

"Current month" on both the homepage and each flat's page is the latest
month where at least half the active portfolio has income recorded — not
literally today's calendar date, since this is a hand-updated ledger and the
real current month is usually still an empty placeholder for the first few
weeks.

## What's deliberately not imported

The source workbook's `Logins & Providers` sheet (account credentials) is
never read by the import script and never will be — that doesn't belong in
a database a web app queries. Everything else in the workbook is imported:
every flat's Summary table, its itemized OPEX/CAPEX/Bookings Income
breakdown, the Amazon/Temu purchase-detail sheets, the Main Page sheets'
shared overhead costs, and the TARGETS sheet's goals. The one thing left as
spreadsheet-only: each property-year sheet's redundant income-by-month rows
on the Main Page sheets, which just repeat numbers each flat's own Summary
table already has.

## Security notes

- `data/dashboard.db` and `data/uploads/` are gitignored — financial data
  and uploaded documents are never committed.
- This is a local single-user tool with no authentication — don't expose
  port 5050 beyond your own machine.
