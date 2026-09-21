# Urban Nest Dashboard

A portfolio operating and analytics system for a UK short-term-rental business.

Built to replace a spreadsheet-heavy monthly reporting workflow with one structured system for bookings, expenses, documents, targets, portfolio analysis and reporting.

**Live demo:** https://urban-nest-dashboard-vercel.vercel.app

> The public demo uses synthetic data. Client financial records, uploaded documents, credentials and the production database are not included in this repository.

**Status:** Active development

## What it does

Urban Nest Dashboard combines four jobs in one system:

- **Monitor** — portfolio and property KPIs at a glance
- **Analyse** — historical trends, comparisons, occupancy, ADR, RevPAR and expenses
- **Record** — bookings, transactions, documents and historical information
- **Act** — review uploaded documents, identify missing data and investigate unusual performance

### Current features

- Portfolio revenue, profit, occupancy, ADR and RevPAR analytics
- Property-level workspaces
- Reservation and booking-statement imports
- Portfolio booking calendar and occupancy analysis
- Opex / Capex and vendor/category expense analysis
- Transaction ledger
- Document Inbox with extraction, review and confirmation
- Revenue and profit targets with historical context
- Data-completeness monitoring
- PDF, Excel and CSV reporting
- Shared-overhead tracking

## Document workflow

```text
Upload
  ↓
Extract
  ↓
Review
  ↓
Correct / classify
  ↓
Confirm
  ↓
Bookings / transactions
  ↓
KPIs and reports update
```

PDFs and images can optionally use Claude for extraction. CSV and XLSX files are parsed directly.

Nothing extracted from a document is silently added to the ledger without review.

## Architecture

**Stack:** Python, Flask, Jinja2, SQLite, Chart.js, HTMX, Alpine.js and optional Anthropic API integration.

The application uses normalized bookings, transactions, documents and property records instead of relying on spreadsheet-style cached monthly totals.

KPIs are calculated through one central metric layer, including:

```python
revenue()
costs()
net_profit()
occupancy()
adr()
revpar()
booked_nights()
monthly_series()
```

Missing data is not treated as zero, and important financial records remain traceable to their underlying source.

## Running locally

```bash
git clone https://github.com/DadoCode/urban-nest-dashboard.git
cd urban-nest-dashboard

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

python3 dashboard/app.py
```

Then open:

```text
http://localhost:5050
```

For optional AI document extraction:

```bash
cp .env.example .env
```

Then add:

```text
ANTHROPIC_API_KEY=...
```

## Privacy

The public repository excludes:

- production databases
- uploaded invoices and statements
- client financial records
- private documents
- credentials
- API keys

The live demo uses synthetic data.

## Development

This project was built iteratively around a real operational workflow rather than from a generic dashboard template.

I use AI coding tools, including Claude Code, to accelerate implementation while I define the product requirements, architecture, business rules, testing approach and UX direction.

The commit history shows the progression from spreadsheet migration and normalized data modelling through document ingestion, booking analytics, reporting and the current UX redesign.

### Current focus

The current development pass is focused on:

- improving visual hierarchy and reducing data overload
- making charts easier to analyse
- connecting high-level metrics to underlying records
- improving target visualisation
- redesigning document review
- making filtering and navigation consistent throughout the product
