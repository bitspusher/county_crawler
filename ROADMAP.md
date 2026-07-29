# Roadmap

Derived from [MVP.md](MVP.md). Section references (§) point there.

**MVP deliverable:** a local SQLite dataset plus CLI reports. No service, no UI, no
email pipeline. The weekly output of §7 is produced by running a command and reading
the result.

**MVP done means:** one month of NOTS and TDUS collected end to end, rescissions and
cancellations joined out, the §11926 zero-tax split validated or replaced, derived
prices spot-checked including Stockton, parsers under test, and `--report` emitting
Section A and Section B in the shape §7 describes.

---

## Where the code is against the spec

Current tree: `probe_eagle.py` (the §5.1 discovery probe, historical — targets the S6
basic form), `collect_sjc.py` (the collector).

### Implemented and matching the spec

| Spec | Where |
|---|---|
| Two-step search, integer pagination (§5.1) | `Portal.search`, `collect_sjc.py:365` |
| Doc type IDs 41 / 22, live vocabulary with fallback (§4) | `KNOWN_IDS`, `Portal.doctype_vocab` |
| Append-only observations with `observed_at` (§6) | `index_obs`, `detail_obs`, `party_obs` |
| Price derived in a view, never stored (§6, §6.4) | `v_auction_sales.derived_price` |
| §11926 sale classification from $0 tax (§6.5) | `v_auction_sales.sale_class` |
| Repeat-buyer grouping (§8) | `v_repeat_buyers` — exact-string, as the spec notes |
| Text-line parsers over `ss-search-row`, not markup structure (§6.2) | `parse_results`, `parse_detail` |
| Persistent browser profile, manual CAPTCHA clearance (§5.1, §10) | `PROFILE`, `--headed` gate |
| Serial requests with a sleep between each (§10) | `Portal._pause`, `--delay` |
| Index and detail metadata only, no paid images (§5.1) | no `/Web/cart` path anywhere |

### Gaps — spec says it, code does not do it

1. **Rescissions and cancellations are not collected.** `DOCTYPES` holds only NOTS and
   TDUS, and `v_upcoming` selects NOTS with no exclusion join. §3 calls this a
   correctness requirement, not a feature — as it stands, Section A would publish
   auctions that will not happen.
2. ~~**No parser tests.**~~ **Partially closed.** There are now 109 tests, including
   both parsers, `Portal.search` driven through a fake page, the derivation views over
   an in-memory DB, and the date-window logic. But §6.2 asks for **captured** fixtures
   and all five are hand-authored, so the parser tests are a regression lock rather
   than a correctness check. See `tests/fixtures/README.md`;
   `scripts/capture_fixtures.py --headed` closes the rest.
3. ~~**A result cap degrades into "zero rows."**~~ **Closed.** `Portal.search` raises a
   dedicated `ResultCapExceeded`, and `main` catches it *before* the generic
   `RuntimeError` handler, records `RESULT_CAP` in `run_log`, stores nothing for that
   window, and exits non-zero at the end of the run. The label-mode retry is
   unreachable from a cap.
4. ~~**Zero rows warns but still commits.**~~ **Closed.** An unexpected zero-row window
   aborts the whole run with a diagnostic checklist and a non-zero exit. Aborting the
   run rather than the window is deliberate: a dead session returns zero for *every*
   window. `--allow-zero-rows` is the deliberate escape hatch.
5. **`report()` is not the §7 output.** It is a useful dump — now including a `run_log`
   coverage section — but there is no 7-day window, no Section A / Section B split, and
   no export.
6. **Nothing consumes parcel data (§5.2) or the AG §2924m dataset (§5.3).**
7. ~~**`page_no` is always 0.**~~ **Closed.** `Portal.search` stamps each row with the
   result page it came from and `store_index` reads it off the row. A row parsed outside
   the pagination walk stores NULL rather than a misleading 0.
8. ~~**`run_log` is never written.**~~ **Closed.** `run_log_start` opens a row *before*
   the search runs and `run_log_finish` closes it with counts and a note, so an
   interrupted window still leaves a record. A collected-but-empty window is a row with
   `rows_indexed=0`; a never-collected window is the absence of a row.

### New gap, found while closing the above

9. **`doctype_vocab()`'s key orientation may be inverted.** It builds
   `{it["value"]: it["name"]}`, but `main` then indexes the result by *label* to get an
   id (`vocab[label]`), the way `KNOWN_IDS` is keyed. Only one reading can be right and
   only the live JSON payload says which — so this is deliberately **not** "fixed"
   blind. `scripts/probe_xhr.py` reports the actual item shape and calls it explicitly.
   If inverted, `vocab[label]` raises `KeyError` and every search silently degrades to
   label mode.

### Unresolved, per the spec itself

- **The automation issue (§5.1).** `Portal.xhr` executing `fetch` inside the page with
  `X-Requested-With` is the attempt at header emulation. Whether it actually returns
  JSON and result HTML rather than the app shell is unverified — `doctype_vocab`
  already carries the disclaimer-detection fallback for when it doesn't. Everything
  downstream is blocked here, and the UI-automation fallback is still open.

---

## Phase 0 — no code required

Both items are §11 steps 4 and 6: the best ratio of information gained to effort spent,
and neither waits on the automation issue.

- [ ] **Call the Recorder about bulk data** (209-468-3939). Could obsolete the whole
      portal path and the parcel bridge at once. CPRA under Gov. Code §6253.9 if no
      product exists. (§10, §11.6)
- [ ] **Start repeat-buyer analysis from the AG §2924m dataset.** Winning bidder names,
      no scraping. Answers "who would I even call" today. (§5.3, §8, §11.4)
- [ ] Review the portal terms, including the indemnification clause. (§10)
- [ ] Check the SB 272 Enterprise System Catalog. (§10)

## Phase 1 — unblock collection

- [ ] **Resolve the automation issue.** Confirm whether `Portal.xhr` returns real JSON
      and result HTML. If not, switch to UI automation — fill, click Search, read the
      DOM, page through. ~30 records/month makes the slow path affordable. (§5.1, §11.1)
      **Run `python scripts/probe_xhr.py --headed`** — it drives the real `Portal` and
      prints a verdict, plus dumps the raw responses to `debug/xhr_probe/`. Needs a human
      to clear the CAPTCHA once. This is the only item here nobody else can do.
- [x] Make a result cap fatal for that window rather than falling through to the
      label-mode retry. (Gap 3)
- [x] Make an unexpected zero-row window fail loudly instead of committing. (Gap 4)
- [x] Put `parse_results` / `parse_detail` under test. (Gap 2 — tests exist; the
      fixtures are still synthetic, see below)
- [ ] **Capture live-markup fixtures**, replacing the synthetic ones, so the parser
      tests become a correctness check. `python scripts/capture_fixtures.py --headed`.
      Expect failures on the first real capture — that is the signal. (§6.2, Gap 2)
- [x] Record the real `page_no`. (Gap 7)
- [x] Write `run_log` rows so collected windows are distinguishable from empty ones.
      (Gap 8)
- [ ] Settle `doctype_vocab()`'s key orientation from the live payload. `probe_xhr.py`
      reports it. (Gap 9)

## Phase 2 — validate the two derivations

One ~31-record TDUS pass answers both.

- [ ] **Run one month of TDUS with details.** (§11.2)
- [ ] **Check the §11926 split.** Expect bimodal: a cluster at exactly $0.00, a spread
      of real values. If everything carries tax, fall back to grantee-name
      classification and rewrite `sale_class`. (§6.5, §8.1)
- [ ] **Spot-check derived prices against known sales**, including at least one Stockton
      property, to settle the charter-city question. If the recorder's Tax Amount
      captures only the county's $0.55, Stockton is wrong by 2× and
      `DTT_RATE_PER_1000` needs to become city-dependent. It lives in a view, so this
      is a rebuild, not a re-scrape. (§6.4, §11.3)

## Phase 3 — make Section A correct

- [ ] Collect `Rescission Of Default` and `Cancellation/Termination`; resolve their doc
      type IDs from the live vocabulary. (§3, Gap 1)
- [ ] Join them out of `v_upcoming` by matching party and recording sequence.
- [ ] State the residual risk in the output: oral postponements at the sale never reach
      the recorder, so no amount of collection catches them. (§3)

## Phase 4 — the weekly output

- [ ] Section A from `v_upcoming`, Section B from `v_auction_sales` over the previous
      7 days, in the field order of §7.1 and §7.2, with unavailable fields shown as
      unavailable rather than omitted. (§7)
- [ ] CSV export alongside the terminal report.
- [ ] Carry the assessed-value and finding-aid caveats into the output text. (§5.1, §7.2)
- [ ] Backfill history — ~12 requests per doc type per year makes this cheap. (§5.1, §7.3)

## Phase 5 — the parcel bridge

Until one of these lands this is a transaction feed, not a property tracker (§1, §6.3).

- [ ] **Published legal notices first** — APN, address, and opening bid, and opening bid
      has no other source. (§5.4)
- [ ] **Owner-name → assessor parcel join second.** Free and immediate, messy. (§6.3.1)
- [ ] **Score both against the AG dataset** as ground truth. (§5.3)

---

## Deliberately parked

Kept here so they are not drifted into. Full reasoning in §9.

- `Default` collection plus equity estimate — highest-value next step after MVP, 81/mo.
- Scoring and ranking — as a view, never baked into collection.
- Tax-default collector (§5.5).
- Prediction — needs history that does not exist yet.
- Homeowner-facing product — different company, different face. Do not build both.
- Multi-county expansion — eased by the Eagle pattern, but §7.3's thin volume may make
  it a precondition rather than an extension.

## Named mistakes not to make

- **Do not collect `Substitution Of Trustee`** (1016/mo). It tracks payoffs, not
  distress, and would swamp the product in false positives. (§3)
- **Do not defeat the CAPTCHA.** Solve it manually, once per profile. (§10)
- **No LLM at runtime.** Deterministic parsers fail loudly; models fail silently and a
  misread digit quietly poisons the database. (§6.2)
- **Do not store derived values.** Price and sale class live in views so assumptions can
  change without re-collection. (§6)
