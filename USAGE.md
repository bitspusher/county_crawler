# Usage

How to run the collector locally, start to finish. Context for *why* any of this works
the way it does is in [MVP.md](MVP.md); what's still missing is in [ROADMAP.md](ROADMAP.md).

---

## 1. Install

Python 3.10+ and a Chromium that Playwright drives.

```sh
cd county_crawler
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -e '.[dev]'            # collector + tests + lint (pyproject.toml)
python -m playwright install chromium
```

Playwright is the only runtime dependency; everything else is stdlib. The
`[dev]` extra adds pytest/ruff/mypy — skip it (`pip install -e .`) if you only
want to collect.

Verify:

```sh
make test          # 150+ tests, no network, a few seconds
```

Playwright is only needed to actually collect — the whole test suite, the
report (`--report`), and the metrics run without it. If it is missing, only
`main()` complains, at the point of use.

---

## 2. First run — clear the gate by hand

The portal sits behind a Google reCAPTCHA and a disclaimer page. **Clearance persists per
browser profile, not per session.** The collector keeps a persistent Chromium profile in
`./.browser_profile`, so this is an occasional chore, not a per-run one.

The first run must be headed:

```sh
python collect_sjc.py --start 2026-06-01 --end 2026-06-30 --headed
```

A browser window opens. If the disclaimer or CAPTCHA appears, the script stops at:

```
>>> Clear the disclaimer/CAPTCHA, then press Enter...
```

Solve it in the window, then press Enter in the terminal. Collection proceeds.

**Solve it manually. Do not automate around it** — see MVP.md §10.

Headless runs work once the profile carries the clearance. If the profile is fresh or
the clearance has lapsed, a headless run exits immediately with
`Disclaimer/CAPTCHA gate. Rerun with --headed and clear it.` That is the signal to do
one headed run again.

Never commit `.browser_profile/` — it holds live session cookies. It's in `.gitignore`.

---

## 3. Collecting

```sh
python collect_sjc.py --start 2026-06-01 --end 2026-07-31
```

Date range is inclusive and gets split into **3-day windows** internally
(`--window-days`). Every window is an **unfiltered sweep** — the portal silently
ignores the doc-type filter (measured 2026-07-29) — and a full unfiltered month
is ~130 pages, which trips the portal's result cap. The collector keeps only the
four in-scope types (`KEEP_DOCTYPES`: NOTS, TDUS, Rescission Of Default,
Cancellation/Termination) on the way into the database, and fetches details only
for TDUS. You can pass a year; you don't need to loop yourself.

### Flags

| Flag | Default | What it does |
|---|---|---|
| `--start YYYY-MM-DD` | 30 days ago | Start of the recording-date range |
| `--end YYYY-MM-DD` | today | End of the range, inclusive |
| `--types TDUS,NOTS` | `TDUS,NOTS` | **Inert** — kept for compatibility. The portal ignores the doc-type filter, so every sweep is unfiltered and selection uses `KEEP_DOCTYPES` |
| `--window-days N` | `3` | Days per search window. A month unfiltered trips the result cap; 3 days is ~14 pages |
| `--max-pages N` | `40` | Refuse a window needing more pages than this (read from the POST's `totalPages` before any page is fetched) |
| `--allow-zero-rows` | off | Accept a zero-row window instead of aborting. Only after checking WHY — a dead session also returns zero |
| `--no-detail` | off | Index only. Skips the per-document detail fetch — **no tax amount, so no derived price** |
| `--limit-details N` | `0` (no limit) | Fetch details for only the first N documents per window. Good for a smoke test |
| `--headed` | off | Show the browser. Required whenever the gate needs clearing |
| `--delay SECONDS` | `1.5` | Sleep between every request. Leave it alone or raise it |
| `--report` | off | Read the DB and print. No network at all |
| `--debug` | off | Dump raw responses to `./debug/` |

Every window commits as it finishes, so an interrupted run keeps everything
collected so far, and `run_log` records exactly which windows completed —
re-run the same range to fill holes; duplicates are harmless (see §4).

### What a run costs

Requests are serial by design. Sweep arithmetic: ~10 windows per month × (1 POST
+ ~13-14 result pages) ≈ **150 index requests per month of range**, plus ~31 TDUS
detail fetches — budget roughly **4-5 minutes per month collected** at the
default delay. A one-year backfill is ~1,800 index requests plus ~370 details:
about an hour in one headed session. Index-only runs (`--no-detail`) skip the
details but not the sweep.

### Suggested first pass

Validate the pipeline on a small slice before backfilling:

```sh
python collect_sjc.py --start 2026-06-01 --end 2026-06-03 --limit-details 2 --headed --debug
```

Then a real month (June 2026 was collected this way on 2026-07-29):

```sh
python collect_sjc.py --start 2026-06-01 --end 2026-06-30 --headed
```

---

## 4. Reading the results

```sh
python collect_sjc.py --report
```

Read-only, no network, safe to run any time. The report leads with the two
sections that ARE the product (§7):

- **SECTION A — COMING UP FOR AUCTION**: notices recorded, newest first, with
  the parties on each notice. Address, APN, and auction date print as
  `unavailable` because the recorder index does not carry them — and the party
  list mixes the homeowner with the foreclosure trustee, unlabelled, so the
  column deliberately does not say "owner".
- **SECTION B — JUST CLOSED**: completed sales with `derived_price` (computed
  from transfer tax — not a recorded number) and `sale_class`
  (`likely_third_party` = real tax paid; `likely_reversion` = $0.00 tax, read
  as the lender taking the property back; `unknown` = tax unreadable).

Then a DIAGNOSTICS block: counts, the reversion/third-party split, sample
derivations, **collection coverage** (which windows were actually collected —
an empty month and a never-collected month look identical without it), and
repeat buyers.

**What the numbers do and don't claim right now:**

- The §11926 zero-tax split is *supported on one collected month* — lenders and
  investors separated cleanly in June 2026 — not proven.
- Derived prices assume $1.10/$1,000 county-wide. That is **unverified for
  Stockton**: if the recorder's Tax Amount captures only the county's half
  there, Stockton prices are understated 2× and nothing in this data can detect
  it (ROADMAP Phase 2).
- Section A still lists sales that will not happen — rescissions and
  cancellations are collected but not yet joined out (ROADMAP Phase 3).

Or query `sjc.db` directly. **Use the views, not the tables** — the tables are raw
append-only observations with duplicates by design:

```sh
sqlite3 sjc.db "SELECT doc_number, recording_date, derived_price, sale_class
                FROM v_auction_sales ORDER BY recording_date DESC LIMIT 20;"

sqlite3 sjc.db "SELECT * FROM v_repeat_buyers LIMIT 20;"
```

| View | What it gives you |
|---|---|
| `v_auction_sales` | Completed sales, with `derived_price` and `sale_class` |
| `v_upcoming` | Recorded notices of trustee's sale |
| `v_repeat_buyers` | Grantees by purchase count — the customer list |
| `v_latest_index` / `v_latest_detail` | Most recent observation per document |

### Re-running is safe

Collection is append-only; nothing is updated in place. Running the same window twice
inserts a second set of observations rather than overwriting, and the `v_latest_*` views
resolve to the newest. To start clean, delete `sjc.db` — the schema and views are
recreated on every run.

Because price and sale class are computed in views, **changing an assumption never means
re-collecting.** Edit `DTT_RATE_PER_1000` or the `VIEWS` block in `collect_sjc.py` and
run `--report` again; the views are dropped and rebuilt at open.

---

## 5. Troubleshooting

Every message below is current as of 2026-07-29. The collector's design rule is
that **nothing fails silently**: a response that looks like success but carries
no data raises, a capped window stores nothing and exits non-zero, and a
zero-row window aborts the whole run.

**`disclaimer/CAPTCHA gate. The session is not authenticated`** (run exits)
The profile lost its clearance. Re-run with `--headed` and clear the gate. The
collector detects this by response *shape*, not just status code, so it stops
at the first dead-session response instead of grinding through every window
collecting nothing.

**`app shell, not a data fragment. Header emulation was not accepted`** (run exits)
The portal served its SPA wrapper instead of data. One endpoint doing this is
known and harmless (the doc-type vocabulary — hardcoded IDs cover it); the
search path doing it means the portal changed. Re-probe (§6).

**`window needs N pages, over the 40-page limit — narrow it`**
The POST reports the result-set size up front (`totalPages`) and the collector
refuses over-wide windows before fetching a single page. Use a smaller
`--window-days`. Nothing was stored for that window and the run exits non-zero
at the end, so a capped window can never masquerade as an empty one.

**`!! ZERO ROWS for the sweep ...`** (the run ABORTS)
Take it seriously. An unfiltered 3-day window should return hundreds of
documents, so zero means the session lapsed or the portal changed — not an
empty calendar. Nothing is stored for the window; the abort message carries a
checklist. If a window is *genuinely* empty (verified by hand), re-run with
`--allow-zero-rows`.

**`! vocabulary fetch failed ... using known IDs`**
Expected and harmless — that endpoint always rejects the header emulation. The
hardcoded IDs (41/22) cover it, and the doc-type filter is server-ignored
anyway.

**`keeping N in scope: {...}`**
Normal. The sweep retrieves every document type in the window (the portal
ignores the type filter); this line reports what survived `KEEP_DOCTYPES`.
~490 documents in, ~36 kept is typical for 3 days.

**`[n/N] 2026-0xxxxx FAILED: ...`**
One detail fetch failed. It's stored with `fetch_ok=0` and excluded from
`v_latest_detail`, so it won't poison prices. Re-run the window to retry.

**Hanging with no output**
Don't wait on network idle — the portal runs a session heartbeat that never
settles (MVP.md §5.1). The collector already avoids this, but it's the first
thing to suspect in new code.

---

## 6. Probes — for when the portal changes or a question needs settling

All probes are read-mostly, cost a handful of requests, and store nothing in
the database. Each needs `--headed` the first time so a human can clear the
CAPTCHA.

| Probe | Question it answers |
|---|---|
| `scripts/probe_xhr.py` | Does the transport still work, and is the doc-type filter still ignored? Run after any suspected portal release. |
| `scripts/probe_doctype_filter.py --manual` | Which value format (if any) makes the doc-type filter stick — fill the field in the real UI and it reads back what the widget produced. If a format works, collection drops from ~140 pages/month to ~2. |
| `scripts/probe_rescission_join.py` | ROADMAP Phase 3's open question: do rescission/cancellation details reference the original document number (exact join), and what fraction reference a NOTS at all (real urgency)? Needs a collected `sjc.db`. |
| `scripts/capture_fixtures.py` | Replaces the synthetic test fixtures with live markup. Redact individual names per AI_CONTEXT.md rule 11 before committing. |

`probe_eagle.py` is the original discovery tool that mapped the endpoints. You
only need it if the collector breaks in a way the probes above can't explain.

```sh
python probe_eagle.py --headed --slow --manual-nav
```

It hands you the browser, you drive to a result page, and it harvests every `<select>`,
table, and field on whatever page you leave it on — plus a full HAR and every XHR body —
into `./probe_out2/`. Start with `report.md` and `network_calls.json`.

`--chrome` uses your installed Chrome instead of bundled Chromium.

Note it drives the **S6** basic search form, while the collector uses **S7** advanced
(S6 doesn't expose the document-type filter). Keep that in mind when comparing field
names.

---

## 7. Contributing

Two documents are binding before any change:

- **[AI_CONTEXT.md](AI_CONTEXT.md)** — the hard rules. Written for agents, holds
  for humans. The ones people trip over: never store a derived value (views
  only), the observation tables are append-only, and **individual party names
  never go into tracked files** (rule 11 — document numbers instead; the data
  names real people in active foreclosure).
- **[DEVELOPMENT.md](DEVELOPMENT.md)** — tests, markers (`live` never runs by
  default; a test run must never become a crawl), the regression gate, and the
  agent pipeline (currently parked — do not install the crontab casually; the
  machine's crontab is shared with another project).

`make check` is the gate: ruff + format + mypy + the non-live suite.
