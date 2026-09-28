---
name: urban-nest-product-design
description: Product-design and simplicity rules for the Urban Nest dashboard. Use for any UI, UX, chart, table, navigation, or financial-presentation work.
---

# Urban Nest product design

Urban Nest is a solo-operator short-let business dashboard, not a
customer-facing product. The bar is not "impressive" -- it's "Dado opens
it and understands the business in 5 seconds."

## The rule that matters most

**Delete before adding.** For every new element, ask:
1. Is this already shown somewhere else?
2. Does it help make a decision?
3. Is this the right level of detail for this page?
4. Can something be removed instead?

A prettier page that takes longer to understand is a regression. When in
doubt, cut it -- Dado has explicitly and repeatedly said he wants this
tool simpler, not more feature-complete. Do not add a section just
because the backend can calculate the number.

## Avoid

- Generic SaaS dashboard look (giant typography, rainbow charts, excess
  badges, shadow-heavy nested cards)
- Repeating a metric that's already shown elsewhere on the same page
- Explanatory paragraphs the interface should make obvious on its own
- A chart with more colours/series than the question actually needs
- Building a whole new section for data that isn't clean/complete yet
  (an empty or near-empty feature reads as broken, not "future-ready")

## Reuse before building

The design system already exists -- extend it, don't reinvent it:
- Tokens/type-scale/spacing: `dashboard/static/style.css` (`:root`)
- Chart defaults/colours: `dashboard/static/charts.js` (`UN.*`, `quietScales`)
- Shared Jinja macros: `dashboard/templates/_components.html`
  (`kpi_tile`, `section`, `empty_state`, `tabs`, `progress`, `delta`)

## Financial presentation

- A label must match what the number actually is. "Revenue" for a
  managed flat means the management fee Urban Nest earns, not the
  flat's gross booking revenue -- if a label and the underlying
  calculation disagree, say so before changing either.
- Never change a KPI formula, revenue/expense recognition, or
  management-fee logic as part of a presentation/labelling task. Report
  it and get an explicit go-ahead first.
- Prefer removal/relabelling over inventing a new derived metric.

## Charts and tables

- One clear question per chart. Minimise series; avoid a legend when a
  chart only needs 2-3 concepts (see Portfolio Performance: revenue up,
  costs down, profit line -- no per-property colour stack, that was
  tried and explicitly rejected as unreadable).
- If a chart needs more than ~5-6 categories to compare, small
  multiples (one compact card per item) usually reads better than one
  crowded multi-line chart -- also already tried and preferred.
- No bezier smoothing on line charts between real data points -- it
  invents a curve that didn't happen. Straight segments only.
- Tables: right-align numbers, tabular numerals, compact rows, no
  column that duplicates another column's information.

## Before a substantial UI change

1. Read the existing route + template for the page first -- most of
   what's needed already exists somewhere in this codebase.
2. State what you'd remove or demote before proposing additions.
3. Implement with the existing token/macro/chart system.
4. Open the real rendered page in the browser (built-in browser tools
   already available -- no separate MCP server needed) and check it at
   mobile and desktop width before calling it done.
5. Confirm in the same turn that no KPI figure moved (a before/after
   snapshot diff, the pattern already used throughout this project).

A change is not done because the code compiles or a route returns 200 --
it's done once you've actually looked at the rendered page.
