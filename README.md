# Urban Nest Estates — Business Dashboard

A local operating system for the portfolio, not a prettier spreadsheet:
every KPI (revenue, profit, occupancy, ADR, RevPAR) is *derived on read*
from normalized `bookings` and `transactions` tables — nothing is cached as
a pre-computed total, so a month with no data simply doesn't appear in a
chart instead of drawing a false zero. A homepage, a page per flat, an
Occupancy and an Expenses view, and a **Document Inbox**: drag in whatever
came in this month (Amazon/Temu orders, cleaning invoices, booking-platform
statements, bank statements, receipts — PDF, photo, CSV or XLSX), review
what got extracted, confirm, and every page updates immediately. Backed by
`data/dashboard.db` (SQLite) — no PriceLabs or other live API involved.

## Run it

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python3 dashboard/app.py        # http://localhost:5050
```

There's no always-on server — this only runs while that command is running.
Leave the terminal tab open for as long as you want the dashboard reachable;
`Ctrl+C` stops it. Optional: copy `.env.example` to `.env` and set
`ANTHROPIC_API_KEY` to turn on AI extraction for uploaded PDFs/photos (CSV
and XLSX are parsed directly, no key needed) — without a key, uploads still
work, you just fill in the line items by hand afterwards.

## Files

- `dashboard/app.py` — routes for the homepage, Occupancy, Opex & Capex, the
  Documents Inbox, each flat's page, adding an apartment, goals, manual
  transactions, document upload/review/confirm, and calendar sync.
- `dashboard/kpis.py` — **the one place every page gets its numbers from.**
  `revenue()`, `costs()`, `net_profit()`, `occupancy()`, `adr()`, `revpar()`,
  `booked_nights()`, `monthly_series()` — all computed on the fly from
  `bookings` + `transactions` for whatever property and date range is asked
  for. No other module reads those tables directly for reporting.
- `dashboard/db.py` — the normalized SQLite schema and connection helper.
- `dashboard/extraction.py` — line-item extraction: Claude vision for
  PDF/image (also guesses which flat and which month a document belongs to
  from anything printed on it), a deterministic column-guessing parser for
  CSV/XLSX (no LLM, no key needed).
- `dashboard/ical_sync.py` — fetches and parses a listing's booking-calendar
  export URL into real reservation rows (check-in/check-out), deduplicated
  against what's already synced.
- `dashboard/templates/`, `dashboard/static/style.css` — the pages.
- `scripts/import_excel_tracker.py` — one-time/repeatable import from the
  real accounts tracker (.xlsx, one sheet per flat per year) into the
  *original* cache-table shape.
- `scripts/migrate_to_normalized_schema.py` — transforms that into the
  normalized `bookings`/`transactions`/`targets` shape `kpis.py` reads,
  including a reconciliation pass so summed line items always match the
  workbook's own trusted totals exactly. **Run this after re-running the
  Excel import**, in that order.
- `scripts/drop_legacy_tables.py` — drops the old cache tables once the
  migration's printed `verification OK`. Already done for the live database;
  only needed again after a future re-import.

## The data model

Five tables, `dashboard/db.py`:

- **`properties`** — a flat (`type='flat'`) or the `general-overheads`
  cost-centre (`type='overhead'`, kept out of the homepage's per-flat grid
  and the Occupancy/ADR math). Holds `ical_url`/`ical_synced_at` and a
  `start_date`.
- **`property_fixed_costs`** — recurring budget lines (rent, council tax,
  management fee) per flat.
- **`bookings`** — reservation-level: `check_in`/`check_out`, platform,
  gross/fees/net revenue, `status`. Real reservations from calendar syncs
  and booking-statement uploads live here; historical months where the
  source spreadsheet's per-reservation detail wasn't recoverable get one
  synthetic row per month carrying the real `days_booked` figure, so
  occupancy/ADR/RevPAR derive through the exact same code path either way.
- **`transactions`** — every other line item, income or expense
  (`direction`), with a `category` and a `capex` flag. Tagged `source`:
  `excel_import` / `manual` / `upload`.
- **`documents`** — uploaded files: detected flat/period, extraction
  status, confidence, the raw extracted JSON for traceability back to source.

## The ongoing monthly workflow — the Document Inbox

`/documents`: drop in whatever came in this month. The system saves the
file, extracts line items, tries to guess which flat and month it belongs
to (from anything printed on the document itself, or from the flat you
picked at upload time), and flags anything that looks like it might already
be on file. Nothing is written to `transactions` until you review the
extracted table and hit **Confirm** — you can fix any field, bulk-reassign
the flat or category for several rows at once, or untick a row to skip it
entirely. Every confirmed line item keeps a `document_id` back-reference to
its source file. The same review/confirm flow is also reachable per-flat
(that flat's page has its own upload form, for when you already know where
something belongs).

Separately, each flat's page has a **booking calendar sync**: paste its
Airbnb "Export calendar" link (or Booking.com/Vrbo's sync-calendars URL) and
hit Sync to pull in real reservations (`bookings` rows with actual
check-in/check-out dates), deduplicated against previous syncs.

The one rule threading through all of this: a month that already has a
*real* (non-placeholder) `excel_import` figure is never overwritten by a
manual entry, an upload, or a calendar sync — that historical ledger stays
authoritative; new data only fills in what it doesn't cover.

"Current month" everywhere is the latest month where at least half the
active portfolio has recorded income — not literally today's calendar date,
since this is still substantially hand-fed data and the real current month
is usually still empty for the first few weeks.

## A real finding from the rebuild

Migrating to derive-from-line-items (rather than trust the spreadsheet's
own monthly total cells) surfaced **real revenue the old cached-totals view
was silently missing** — several property/months had itemized booking
income entered in the workbook's "Bookings Income" block that was never
rolled up into that month's Summary-table total (a gap in the manual
spreadsheet process, not a bug in either version of this dashboard).
Portfolio revenue for August 2026, for example, is **£46,656**, not the
**£36,264** the previous cache-table version showed. Worth treating past
monthly figures you'd memorized as superseded.

## What's deliberately not imported

The source workbook's `Logins & Providers` sheet (account credentials) is
never read by the import script and never will be. Everything else is
imported, including the itemized OPEX/CAPEX/Bookings Income breakdown, the
Amazon/Temu purchase-detail sheets, the Main Page sheets' shared overhead
costs, and the TARGETS sheet's goals — except each property-year sheet's
redundant income-by-month rows on the Main Page sheets, which just repeat
numbers each flat's own Summary table already has.

## Security notes

- `data/dashboard.db` and `data/uploads/` are gitignored — financial data
  and uploaded documents are never committed.
- This is a local single-user tool with no authentication — don't expose
  port 5050 beyond your own machine.

## Roadmap (see the approved architecture plan for full detail)

Phase 0 (normalized schema + derived KPIs) and Phase 1 (Document Inbox) are
done. Not yet built: the Overview page's global date-range selector and
insights engine, the Occupancy page's ranked-bars/heatmap redesign (today
it's still a per-flat line chart, now at least null-safe), the Expenses page
rename/rebuild with vendor breakdown and filters, the Add-Property wizard,
data-completeness indicators, and a Properties index page to replace the
sidebar's permanent flat list once the portfolio outgrows it.
