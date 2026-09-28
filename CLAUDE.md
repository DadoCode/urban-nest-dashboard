# Urban Nest dashboard

A solo-operator short-let business dashboard (Flask + Jinja + SQLite).
Real business data lives in the gitignored `data/dashboard.db` -- this
repo is code only.

## Working style

- Simplicity over features. Dado has repeatedly asked for this tool to
  get *simpler*, not more impressive -- when in doubt, cut a section
  rather than add one. See `.claude/skills/urban-nest-product-design/SKILL.md`
  for the full design rules.
- Commit + push after each piece of work, with the standard
  `Co-Authored-By` attribution.
- Never change KPI math, revenue/expense recognition, or management-fee
  logic as a side effect of a presentation task -- report it first.
- Before touching real financial data: back up `data/dashboard.db`, test
  on a copy, use a transaction, verify totals against the source, only
  then apply to the live file.

## Frontend/UX workflow

For substantial UI/UX work, don't jump straight into code:
1. Read the existing route + template first -- most of what's needed
   already exists in `services/`, `_components.html`, or `charts.js`.
2. State what should be removed or demoted before proposing additions.
3. Implement with the existing design tokens/macros, not new ones.
4. Open the real rendered page in the browser (built-in browser tools --
   no separate Playwright/DevTools MCP needed) and check mobile + desktop
   width before calling it done.
5. Confirm no KPI figure moved (before/after snapshot diff against a DB
   copy).

A page whose code compiles isn't done -- done means it's been looked at
rendered, and something was considered for removal.
