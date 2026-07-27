# Usage

How to run the collector locally, start to finish. Context for *why* any of this works
the way it does is in [MVP.md](MVP.md); what's still missing is in [ROADMAP.md](ROADMAP.md).

---

## 1. Install

Python 3.9+ and a Chromium that Playwright drives.

```sh
cd county_crawler
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install playwright
python -m playwright install chromium
```

There is no `requirements.txt` yet — Playwright is the only third-party dependency.
Everything else (`sqlite3`, `argparse`, `re`, `json`) is stdlib.

Verify:

```sh
python -c "import playwright; print('ok')"
```

If you skip the `playwright install` step, the script exits with
`pip install playwright && python -m playwright install chromium`.

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

Date range is inclusive and gets split into **one-month windows** internally, because the
portal caps result sets (MVP.md §5.1). You can pass a year; you don't need to loop
yourself.

### Flags

| Flag | Default | What it does |
|---|---|---|
| `--start YYYY-MM-DD` | 30 days ago | Start of the recording-date range |
| `--end YYYY-MM-DD` | today | End of the range, inclusive |
| `--types TDUS,NOTS` | `TDUS,NOTS` | Which document types. `TDUS` = completed sales, `NOTS` = upcoming. Unknown names are silently dropped |
| `--no-detail` | off | Index only. Skips the per-document detail fetch — **no tax amount, so no derived price** |
| `--limit-details N` | `0` (no limit) | Fetch details for only the first N documents per window. Good for a smoke test |
| `--headed` | off | Show the browser. Required whenever the gate needs clearing |
| `--delay SECONDS` | `1.5` | Sleep between every request. Leave it alone or raise it |
| `--report` | off | Read the DB and print. No network at all |
| `--debug` | off | Dump raw responses to `./debug/` |

### What a run costs

Requests are serial by design. At ~31 TDUS records in a month, with the default delay
plus the per-detail page settle, budget roughly **1.5–2 minutes per document type per
month of range**. A one-year backfill of both types is well under an hour. Index-only
runs (`--no-detail`) are far faster — a handful of requests per window.

### Suggested first pass

Validate the pipeline on a small slice before backfilling:

```sh
python collect_sjc.py --start 2026-06-01 --end 2026-06-30 \
                      --types TDUS --limit-details 5 --headed --debug
```

Then the real month, which is what MVP.md §11.2 asks for:

```sh
python collect_sjc.py --start 2026-06-01 --end 2026-06-30 --types TDUS
```

---

## 4. Reading the results

```sh
python collect_sjc.py --report
```

Read-only, no network, safe to run any time. It prints document counts, a breakdown by
type, the reversion-vs-third-party split, sample derived prices, and repeat buyers.

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

**`! vocabulary was not JSON; using known IDs` followed by `^ that is the DISCLAIMER
page — session is not authenticated`**
The profile lost its clearance. Re-run with `--headed` and clear the gate. The run
continues on hardcoded doc type IDs (41 / 22), which are usually still correct — but the
rest of the run is likely to return nothing.

**`result cap hit — narrow the date window`**
The window returned more than the portal will serve. Split it. Note the known bug in
ROADMAP.md Gap 3: `main` currently catches this and falls through to a retry, so a capped
window can end up *looking* like an empty one. If you see a cap message anywhere in the
log, don't trust that window's row count.

**`0 rows with numeric id; retrying with the label string`**
Normal fallback. The collector sends the doc type as a numeric ID first and retries with
the literal label. If the retry works, fine.

**`!! ZERO ROWS — expected ~30/month. Investigate before trusting.`**
Take this seriously. Expected volume is ~35 NOTS and ~31 TDUS per month. Zero usually
means the session lapsed or the portal changed. The run does **not** stop and will still
commit — check the log before trusting a report.

**`!! 40 pages, stopping — window too wide`**
Pagination guard. Narrow the range.

**`[n/N] 2026-0xxxxx FAILED: ...`**
One detail fetch failed. It's stored with `fetch_ok=0` and excluded from
`v_latest_detail`, so it won't poison prices. Re-run the window to retry.

**Results look like the app shell rather than data**
This is the open automation issue in MVP.md §5.1. Run with `--debug` and inspect
`./debug/` — `001_results_p1.txt` and `*_vocab_not_json.txt` are the useful ones. The
documented fallback is UI automation.

**Hanging with no output**
Don't wait on network idle — the portal runs a session heartbeat that never settles
(MVP.md §5.1). The collector already avoids this, but it's the first thing to suspect in
new code.

---

## 6. Re-probing after a portal release

`probe_eagle.py` is the discovery tool that mapped the endpoints and field names. You
only need it if the collector breaks in a way that suggests the portal changed.

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
