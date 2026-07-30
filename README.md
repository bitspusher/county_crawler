# county_crawler

San Joaquin County foreclosure tracker: upcoming trustee's sales, and what comparable
properties actually cleared for at recent auctions.

Source is the county recorder's Tyler Eagle portal. The MVP deliverable is a local
SQLite dataset plus CLI reports — no service, no UI.

- [USAGE.md](USAGE.md) — install, run, read the results, troubleshoot. **Start here.**
- [MVP.md](MVP.md) — what this is, what's in scope, and what the data does and doesn't
  contain. Read §6.3 before trusting the word "property": the recorder index carries no
  address and no APN.
- [ROADMAP.md](ROADMAP.md) — phased plan, and where the code currently stands against
  the spec.
- [AI_CONTEXT.md](AI_CONTEXT.md) — hard rules for anyone, human or agent, writing code
  here. Read before your first change.
- [DEVELOPMENT.md](DEVELOPMENT.md) — test suite, the agent pipeline, and how to run both.

## Files

| File | What it does |
|---|---|
| `collect_sjc.py` | The collector. One unfiltered sweep per 3-day window (the portal ignores the doc-type filter — measured 2026-07-29), keeps the four in-scope types via `KEEP_DOCTYPES`, stores append-only observations in `sjc.db`, derives price and sale class in views, and emits the Section A/B report. |
| `probe_eagle.py` | The discovery probe that mapped the portal's endpoints and field names. Historical — it drives the S6 basic form; the collector uses S7. Kept for re-probing after a portal release. |
| `scripts/probe_xhr.py` | Settled ROADMAP Phase 1's automation question (2026-07-29): transport works, doc-type filter is server-ignored. Kept for re-probing after a portal release. |
| `scripts/probe_rescission_join.py` | Phase 3's open question: can a rescission be joined to the notice it kills, and what fraction reference a NOTS at all? Needs `--headed` and a collected `sjc.db`. |
| `scripts/capture_fixtures.py` | Replaces the synthetic test fixtures with live markup, so the parser tests become a correctness check rather than a regression lock. |
| `scripts/collection_metrics.py` | Deterministic project-health numbers. Safe with no database. |
| `tests/` | 156 tests, none of which touch the network by default. See DEVELOPMENT.md. |

## Quick start

```sh
python -m venv .venv && source .venv/bin/activate
pip install -e '.[dev]' && python -m playwright install chromium

python collect_sjc.py --start 2026-06-01 --end 2026-06-30 --headed   # clear the CAPTCHA once
python collect_sjc.py --report                                       # read the DB, no network
```

The portal is behind a reCAPTCHA and a disclaimer; clearance persists in
`./.browser_profile`, so the manual step is occasional rather than per-run. Requests are
serial with a sleep between each (`--delay`, default 1.5s) — keep it that way, see
MVP.md §10.

Full flag reference, run costs, and troubleshooting: **[USAGE.md](USAGE.md)**.

## Reading the output

`sjc.db` is append-only. Every fetch is an observation with an `observed_at`; nothing is
updated in place. Query the views, not the tables:

| View | What it gives you |
|---|---|
| `v_auction_sales` | Completed sales with `derived_price` and `sale_class` |
| `v_upcoming` | Recorded notices of trustee's sale |
| `v_repeat_buyers` | Grantees ranked by purchase count — the customer list |
| `v_latest_index`, `v_latest_detail` | Most recent observation per document |

`run_log` is the other table worth knowing about. It records every
(doc type, window) attempt, so the absence of documents for a month is
distinguishable from that month never having been collected — and a window the
server capped is marked incomplete rather than looking complete. `--report`
prints a coverage section from it.

Sale price is **derived, not recorded**: the index carries no price, so it is computed
from the documentary transfer tax at $1.10/$1,000 (±$500). Two assumptions behind that
number are still unvalidated — the Stockton charter-city rate, and the §11926 zero-tax
split that separates genuine purchases from lender credit-bids. MVP.md §6.4 and §6.5.

Because the derivation lives in a view, revising either assumption is a view rebuild
rather than a re-collection.

## Known limitations

- **No address, no APN.** Not present anywhere in the index or detail views. MVP.md §6.3.
- **No auction date.** The index carries the notice's recording date only.
- **Rescissions and cancellations are not yet collected**, so the upcoming list includes
  sales that will not happen. ROADMAP.md Phase 3.
- The recorder states the grantor/grantee index is a finding aid. Fine for a lead list,
  not for title work.
- **`sjc.db` is local and gitignored** — a fresh checkout has no data until you run
  a collection. June 2026 has been collected end to end (193 documents, 20 TDUS with
  details); the §11926 sale-class split is SUPPORTED on that month, and the
  transfer-tax rate is supported but **unverified for Stockton** — derived prices
  there could be understated 2× and the current data cannot detect it
  (ROADMAP Phase 2).
- **The test fixtures are synthetic.** The suite is a regression lock, not proof the
  parsers handle the county's real markup. `tests/fixtures/README.md` explains why, and
  `scripts/capture_fixtures.py` is how it gets fixed.
