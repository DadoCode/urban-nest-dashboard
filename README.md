# Urban Nest Dashboard

A portfolio operating and analytics system for a UK short-term-rental business.

The project is designed to replace a spreadsheet-heavy monthly reporting workflow with a structured system for bookings, expenses, documents, targets, portfolio analysis and reporting.

**Live demo:** https://urban-nest-dashboard-vercel.vercel.app

> The public demo uses synthetic data. Client financial records, uploaded documents, credentials and the production database are not included in this repository.

**Status:** Active development

---

## What it does

Urban Nest Dashboard combines four jobs in one system:

- **Monitor** — portfolio and property KPIs at a glance
- **Analyse** — trends, comparisons, occupancy, ADR, RevPAR and expenses
- **Record** — bookings, transactions, documents and historical data
- **Act** — review uploaded documents, identify missing data and investigate unusual performance

The goal is not simply to recreate an Excel tracker in a browser. The underlying data is stored as normalized records and the dashboard derives its metrics from those records.

---

## Current features

### Portfolio overview

- Revenue
- Net profit
- Profit margin
- Occupancy
- ADR
- RevPAR
- Booked nights
- Average stay
- Previous-period and historical comparisons
- Portfolio performance trends
- Property performance ranking
- Data-completeness tracking
- Rule-based attention/insight indicators

### Properties

Each property has its own workspace with separate areas for:

- Overview
- Bookings
- Expenses
- Documents
- Settings

This keeps analytics separate from administrative actions and data entry.

### Bookings

- Portfolio booking overview
- Reservation-level records
- Portfolio calendar
- Upcoming stays
- Channel information
- Occupancy analysis
- Occupancy heatmap
- Occupancy vs ADR comparison
- Historical performance

Booking statements can be imported from uploaded files and converted into reservation records.

### Expenses

- Portfolio cost analysis
- Opex vs Capex
- Expense categories
- Vendor analysis
- Shared overheads
- Property-level expenses
- Transaction ledger
- Historical reconciliation records

### Document Inbox

The Document Inbox turns source files into structured financial records.

Supported inputs include:

- booking-platform statements
- invoices
- receipts
- bank statements
- Amazon/Temu orders
- CSV
- XLSX
- PDF
- images

Workflow:

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
KPIs update
