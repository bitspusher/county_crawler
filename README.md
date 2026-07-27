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

## Files

| File | What it does |
|---|---|
| `collect_sjc.py` | The collector. Searches NOTS (doc type 41) and TDUS (22) by month, stores append-only observations in `sjc.db`, derives price and sale class in views. |
| `probe_eagle.py` | The discovery probe that mapped the portal's endpoints and field names. Historical — it drives the S6 basic form; the collector uses S7. Kept for re-probing after a portal release. |

## Quick start

```sh
python -m venv .venv && source .venv/bin/activate
pip install playwright && python -m playwright install chromium

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
