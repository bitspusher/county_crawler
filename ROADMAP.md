# Roadmap

Derived from [MVP.md](MVP.md). Section references (§) point there.

**MVP deliverable:** a local SQLite dataset plus CLI reports. No service, no UI, no
email pipeline. The weekly output of §7 is produced by running a command and reading
the result.

**MVP done means:** one month of NOTS and TDUS collected end to end, rescissions and
cancellations joined out, the §11926 zero-tax split validated or replaced, derived
prices spot-checked including Stockton, parsers under test, and `--report` emitting
Section A and Section B in the shape §7 describes.

*(This definition spans Phases 1–4 — phase checkboxes track steps toward it, and
a checked phase does not claim the MVP is done. Still open against it: the
rescission join, the Stockton spot-check, captured fixtures, and §7's exact
field order.)*

**Item tags:** `[human]` = needs a headed browser with a human-cleared CAPTCHA,
a phone call, or a judgment only the founder can make — the agent pipeline must
not spec these. `[pipeline]` = implementable by the spec pipeline within its
file boundaries. *Enforcement is currently prompt-only (the architect agent's
instructions plus SPEC_TEMPLATE's roadmap-item citation requirement); nothing
mechanical rejects a spec against a `[human]` item yet — which is why it is an
enabling condition for the parked pipeline below.*

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

1. ~~**Rescissions and cancellations are not collected.**~~ **Partially closed:
   collection done, join open — and narrowed to rescissions.** 47 rescissions
   are in the database and `Rescission Of Default` remains in `KEEP_DOCTYPES`.
   `Cancellation/Termination` was dropped 2026-07-30 (see Phase 3): its 83 June
   documents carry no foreclosure signal, so Phase 3's join scope halves. The
   83 already collected stay in the database — the tables are append-only.
   `v_upcoming` still has no exclusion join — §3 calls that a correctness
   requirement, and it hangs on Phase 3's probe.
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
5. ~~**`report()` is not the §7 output.**~~ **Largely closed.**
   `report_sections()` emits Section A / Section B with unavailable fields shown
   as unavailable, and `report()` leads with it. Section A's 7-day window was
   header text only until 2026-07-30 — the `days` argument had no date
   predicate behind it, so a June run printed "last 7 days" above notices
   spanning 06/02–06/30. It now filters, prints the actual date range, counts
   what it excluded, and takes `--report-days` (0 = all).
   Remaining: exact §7.1/§7.2 field order and CSV export (Phase 4).
6. **Nothing consumes parcel data (§5.2) or the AG §2924m dataset (§5.3).**
7a. **Pagination can duplicate, and therefore can skip.** Doc 2026-057938 came
   back twice in one June window, on pages 8 and 9 — 1 repeat in 1,168 rows. The
   duplicate is harmless under append-only, but a boundary that repeats a row can
   also drop one, and a dropped row is invisible: it looks like a document the
   county never recorded. **Mitigated 2026-07-30**, not fixed: `Portal._reconcile`
   compares pages walked and rows swept against the server's `totalPages` after
   each window and records `PAGE_COUNT_MISMATCH` / `ROW_COUNT_SHORT` /
   `DUPLICATE_ROWS` in `run_log.notes`, flagged in `--report`. Costs no extra
   requests (`totalPages` arrives with the searchPost). It detects a short window;
   it does not recover the row.

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
   An inverted vocab now only produces a spurious "NOT IN VOCABULARY — county may have
   renamed it" log line: `main` guards the index and the sweep never consults the
   vocabulary for a query.

### New gaps, found by running it

10. **Section A mixes party roles.** The grantor list carries the foreclosure
    trustee alongside the homeowner — e.g. docs 2026-055449 (homeowner |
    PRIME RECON LLC), 2026-051883 (homeowner | ZBS LAW LLP), 2026-056662
    (homeowner | ENTRA DEFAULT SOLUTIONS LLC); individual names stay in
    `sjc.db` per AI_CONTEXT.md rule 11 — and the index does not label who is
    who. The column has been renamed from "PROPERTY / OWNER" to "PARTIES ON THE
    NOTICE" so it stops asserting a distinction the data does not make, but for
    a lead list the distinction is the point: with no address or APN (§6.3),
    the owner name is the only identifier there is.
    **Committed first pass: the known-trustee name list**, seeded from the June
    data's repeat trustee firms. The token heuristic (DEFAULT / RECON /
    FORECLOSURE / LAW LLP / SOLUTIONS) and the detail view are escalation only
    if the list proves insufficient. Slotted alongside Phase 3, since both
    touch Section A's shape. The trustee is itself worth surfacing — it is who
    you call to ask about a sale — so this is a split, not a discard.

11. **Repeat-buyer analysis needs history, not a month.** All 20 June sales have
    distinct grantees, so `v_repeat_buyers` shows nothing but count-1 rows. §7.3's
    thin-volume warning made concrete: at ~20 completed sales/month the customer
    list only means something across many months. Backfilling is cheap (~10
    windows/month), so this argues for doing Phase 4's backfill early. Note also
    that docs 2026-053614/2026-053613 carry an LLC and an individual buyer with
    visibly related names that do not group — the exact-string limitation of
    §8, observed rather than predicted.

### Resolved since the spec was written

- **The automation issue (§5.1) — SETTLED 2026-07-29.** Header emulation works
  for the transport: `searchPost` returns JSON (including `totalPages`) and
  `searchResults` returns real grid markup at 100 rows/page. Collection runs one
  unfiltered sweep per 3-day window and selects client-side via `KEEP_DOCTYPES`.
  Only the `documentTypes` vocabulary endpoint still serves the app shell;
  `KNOWN_IDS` covers it and it no longer matters. No UI-automation fallback is
  needed. Measured by `scripts/probe_xhr.py`; June 2026 collected end to end on
  this path.
- **The doc-type filter is NOT broken — corrected 2026-07-30.** The 2026-07-29
  "server discards the filter" conclusion was measured with a single-field
  payload. The field is an autocomplete the browser posts as five fields
  (`-holderInput` id, `-holderValue` label, `-searchInput`, `-containsInput`,
  bare field); sending all five filters correctly — June NOTS = 43 docs on one
  page, June TDUS = 20, both pure, against 1,323 docs across 14 pages for an
  unfiltered *three-day* window. The sweep stays the default anyway, because
  `Rescission Of Default` arrives free with it and Phase 3 needs those rows.
  What changes: a filtered month is ~8 requests against ~150 swept, so **wider
  windows are now legitimate** if backfill (Phase 4) makes request count the
  binding constraint. AI_CONTEXT rule 6 previously encoded the bug as the reason
  for 3-day windows, which would have auto-rejected anyone who fixed it; it has
  been reworded to bind on the result cap instead.

---

## Phase 0 — no code required

Six items, none needing code — the two §11 steps (4 and 6) with the best ratio
of information gained to effort spent, plus the two hypothesis tests the
2026-07-29 review found missing and the two legal checks. Nothing here waits on
anything.

- [ ] `[human]` **Call the Recorder about bulk data** (209-468-3939). Could obsolete
      the whole portal path and the parcel bridge at once. CPRA under Gov. Code
      §6253.9 if no product exists. (§10; §11 step 6)
- [ ] `[human]` **Start repeat-buyer analysis from the AG §2924m dataset.** Winning
      bidder names, no scraping. Answers "who would I even call" today.
      (§5.3, §8; §11 step 4)
- [ ] `[human]` **Ask 2–3 repeat buyers whether identifier-less comps are worth
      paying for.** The AG dataset names them. Hypothesis 3 (§2) is the
      load-bearing question and it is answerable by conversation, not code —
      gate Phase 4/5 effort on the answer. *(Added 2026-07-29 review: no
      roadmap item tested this.)*
- [ ] `[human]` **Test the freshness hypothesis** (§2 hypothesis 1) —
      **forward-looking by design**: at the next collection run, take that
      week's fresh NOTS and check PropStream/Foreclosure.com for each on days
      1, 3, and 7, logging first-seen dates as you go. Retro-checking June only
      works if an aggregator displays an explicit listed-on date, and any retro
      sample excludes already-delisted notices (survivorship bias) — say so if
      used. Needs aggregator access (account/trial); budget that first.
      *(Added 2026-07-29 review; redesigned same day — first-listing dates are
      not observable two months later.)*
- [ ] `[human]` Review the portal terms, including the indemnification clause.
      (§10) **Gates Phase 4's backfill and any recurring cadence** — see
      Phase 4.
- [ ] `[human]` Check the SB 272 Enterprise System Catalog. (§10)

## Phase 1 — unblock collection

- [x] **Resolve the automation issue.** Settled 2026-07-29 by two headed runs of
      `scripts/probe_xhr.py`: the transport works, the doc-type filter is
      server-ignored, and the collector now runs unfiltered sweeps with
      client-side selection. Details under "Resolved since the spec was
      written" above. (§5.1, §11.1)
- [x] Make a result cap fatal for that window rather than falling through to the
      label-mode retry. (Gap 3)
- [x] Make an unexpected zero-row window fail loudly instead of committing. (Gap 4)
- [x] Put `parse_results` / `parse_detail` under test. (Gap 2 — tests exist; the
      fixtures are still synthetic, see below)
- [ ] `[human]` **Capture live-markup fixtures**, replacing the synthetic ones, so
      the parser tests become a correctness check.
      `python scripts/capture_fixtures.py --headed` — now defaults to a 3-day
      window; a full month always trips the cap under the unfiltered sweep.
      Expect failures on the first real capture — that is the signal. Captured
      fixtures must be redacted per AI_CONTEXT.md rule 11 before commit.
      (§6.2, Gap 2)
- [x] Record the real `page_no`. (Gap 7)
- [x] Write `run_log` rows so collected windows are distinguishable from empty ones.
      (Gap 8)
- [x] ~~Settle `doctype_vocab()`'s key orientation~~ **Dropped as moot
      (2026-07-29 round 2):** the vocabulary endpoint always serves the app
      shell, so the probe's orientation check can never run — and the fallback
      it always takes, `KNOWN_IDS`, is `{label: id}`, which is exactly how
      `main` indexes it. There is nothing left to settle. (Gap 9)

## Phase 2 — validate the two derivations

**Ran June 2026 on 2026-07-29. 193 documents kept, 20 TDUS with details.**

- [x] **Run one month of TDUS with details.** (§11.2)
- [x] **Check the §11926 split — SUPPORTED on one month; monitoring.** Exactly
      bimodal on n=10 per class: 10 at `$0.00`, 10 carrying real tax. Zero-tax
      grantees are lenders and servicers almost without exception (Fifth Third,
      Freedom Mtg, NewRez/Shellpoint, US Bk Tr, Wilmington Sav Fund, Lakeview Ln
      Serv, DPS Fin, Farmers & Merchants, Jorva Partners); taxed grantees are
      investor entities and individual buyers (SGR Invest, Next Door Neighbor
      Homes, Tier2Keepers, Vanzetti Prop, Neighbor To Neighbor Homes, and
      several individuals — names live in `sjc.db`, not here, per AI_CONTEXT.md
      rule 11). `sale_class` stays as-is. One month is support, not proof, and
      nothing later in the plan re-checks the split, so: (§6.5, §8)
      - [ ] Add a watch to `--report`: flag any $0-tax record whose grantee is
            NOT on an accumulating allowlist of previously-confirmed
            institutional grantees. Not a name-shape classifier — June's own
            data defeats one (a partners-LP sits on the lender list; a trust
            sits off it). `[human]` seeds and extends the allowlist from
            confirmed cases; `[pipeline]` implements the mechanical flagging
            (the list lives next to `KEEP_DOCTYPES`). Doc 2026-055108 is
            EXPECTED to flag — the watch surfaces exceptions, it does not
            reclassify them.
- [ ] **Rate SUPPORTED but unverified for Stockton — the original check refuted
      the wrong direction.** All 10 derived prices land on exact $500
      boundaries, consistent with §6.4's rounding rule at $1.10/$1,000. That
      refutes one failure mode — a *doubled* rate (tax at $2.20, derived
      overstating 2×), under which 3 of 10 halved prices would miss the $500
      grid. But §6.4's actual fear runs the other way: if the recorder's Tax
      Amount captures only the county's $0.55 half, true prices are DOUBLE the
      derived ones — and doubling a $500 multiple always lands on another $500
      multiple, so the boundary check has **zero power** against that case.
      Stockton comps could be published at half value and this data cannot
      detect it. Open until one derived price is compared against a known sale
      (the AG §2924m dataset may supply one), or the parcel bridge lands
      addresses. Being a view, a correction is a rebuild, not a re-scrape.
- [x] **Volumes corroborate §4's table.** June: 43 NOTS (§4 says 35/mo), 20 TDUS
      (31), 47 Rescission Of Default (55), 83 Cancellation/Termination (113).
      Same order of magnitude throughout, TDUS on the low side. (Cancellations
      are no longer collected as of 2026-07-30 — see Phase 3.)

## Phase 3 — make Section A correct

**Scope halved 2026-07-30 — one measurement in, one still open.** June 2026
returned 43 NOTS against 47 Rescission Of Default and 83
Cancellation/Termination. The raw 3:1 ratio overstated the problem, and the
cancellation half of it is now measured at zero: `Cancellation/Termination` is a
generic index label and none of June's 83 touch a foreclosure (details below).
That leaves 47 rescissions against 43 notices. Still unmeasured: what fraction
of those 47 actually kills a NOTS — a `Rescission Of Default` rescinds a
*Default* (81/mo, §4), most of which never reach a NOTS. That fraction, not the
ratio, sets this phase's priority. If it is small, Section A's existing caveat
plus a stated rescission rate may be an adequate MVP answer.

- [x] Collect `Rescission Of Default`. Free with the unfiltered sweep; it is in
      `KEEP_DOCTYPES` and 47 are already in the database. No doc-type IDs
      needed — the sweep is unfiltered. (§3, Gap 1)
- [x] **`Cancellation/Termination` dropped from `KEEP_DOCTYPES` 2026-07-30 —
      it contributes nothing to this join.** The measurement this phase's
      priority was waiting on came back zero for cancellations: all 83 June
      documents are City of Stockton lien releases (34), rooftop-solar UCC
      terminations (36) and homebuilder filings (4), with not one foreclosure
      trustee firm on any of them — no Clear Recon, Prime Recon, ZBS Law,
      Affinia, Entra, National Default, Prestige. Same trap as `Substitution Of
      Trustee`: high volume, wrong signal. This halves the phase's scope; the
      47 rescissions look right (MERS plus a lender in nearly every case) and
      the probe below now runs against those alone.
- [ ] `[human]` **Run the join/referent probe** —
      `scripts/probe_rescission_join.py --headed` (already written, ~6
      requests). It answers BOTH open questions at once: what a
      rescission can be joined ON (an exact document back-reference, or
      nothing), and what fraction reference a NOTS at all — which sets this
      phase's real urgency. `DETAIL_DOCTYPES` fetches TDUS details only today;
      widening it to rescissions is a one-line change costing ~47
      requests/month (not ~130 — cancellations are no longer collected), once
      the probe says it is worth it.
- [ ] `[pipeline]` Join them out of `v_upcoming` once the key is known. Schema
      catch for the implementing spec: `CREATE TABLE IF NOT EXISTS` will not add
      a column to an existing `sjc.db` — an `ALTER TABLE` path is required.
- [ ] `[pipeline]` **If the fallback key (party + recording sequence) is ever
      needed, its prerequisites come first** (and the prerequisites themselves
      are `[human]`-gated where they need ground truth): Gap 10's trustee/homeowner separation (at
      minimum the known-trustee name list, matching on non-trustee names only)
      and a named ground-truth source for the stated error rate (a hand-verified
      sample against detail views, or the AG §2924m dataset). One trustee firm
      spans many unrelated concurrent foreclosures, so matching on raw grantor
      lists would systematically join the wrong notice.
- [x] State the residual risk in the output — oral postponements at the sale never
      reach the recorder. Already carried in Section A's caveats. (§3)

## Phase 4 — the weekly output

**Partially shipped ahead of order:** `report_sections()` in `collect_sjc.py`
already emits Section A and Section B over a 7-day window, with unavailable
fields shown as unavailable and the finding-aid/derivation caveats carried in
the output text (Gap 5 is largely closed). Remaining:

- [ ] `[pipeline]` Exact field order per §7.1/§7.2, including assessed value shown
      as unavailable (it needs parcel data that does not exist yet). (§7)
- [ ] `[pipeline]` CSV export alongside the terminal report. Exports carry the same
      individual-name-handling question as tracked files — AI_CONTEXT.md
      rule 11 governs what may leave the terminal.
- [ ] `[human]` Backfill history — **sweep arithmetic, not the dead filtered-design
      numbers**: ~10 windows/month × ~15 requests ≈ 150/month, ~1,800/year,
      plus ~370 TDUS detail fetches — roughly an hour per backfilled year in
      one headed session at the 1.5s delay. **Gated on the Phase 0 terms
      review**, per §10's "review before automating". (§5.1, §7.3)

## Phase 5 — the parcel bridge

Until one of these lands this is a transaction feed, not a property tracker (§1, §6.3).

- [ ] `[human]` **Published legal notices — discovery first.** Identify the
      adjudicated newspaper(s) carrying San Joaquin County trustee-sale notices
      and whether an aggregator covers them; record the access method (site,
      paywall, format) before designing any collection. The payoff: APN,
      address, and opening bid — and opening bid has no other source. (§5.4)
- [ ] `[pipeline]` **Owner-name → assessor parcel join second.** Free and immediate,
      messy — and dependent on Gap 10's trustee split, or the join runs against
      trustee firm names. (§6.3.1)
- [ ] `[human]` **Score both against the AG dataset** as ground truth. (§5.3)

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
- **The agent pipeline** (`.claude/agents/`, `scripts/*-review.sh`,
  `implement.sh`, `crontab.txt`) — built 2026-07-29, deliberately NOT
  scheduled. Cron stays uninstalled until (a) the Phase 0 items are done,
  (b) `[human]`-tag enforcement is more than prompt-only (the protected-path
  deny-list shipped 2026-07-29 and is no longer a pending condition), and
  (c) the roadmap carries at least 6 open `[pipeline]`-tagged items spanning
  at least two phases — checkable against the checkboxes, so enablement cannot
  self-certify. Until then, run phases manually per-session.
  *(2026-07-29 review: the machinery outsizes the stated MVP deliverable and
  lived entirely outside this priority document — recorded here so it is
  accounted for.)*

## Named mistakes not to make

- **Do not collect `Substitution Of Trustee`** (1016/mo). It tracks payoffs, not
  distress, and would swamp the product in false positives. (§3)
- **Do not defeat the CAPTCHA.** Solve it manually, once per profile. (§10)
- **No LLM at runtime.** Deterministic parsers fail loudly; models fail silently and a
  misread digit quietly poisons the database. (§6.2)
- **Do not store derived values.** Price and sale class live in views so assumptions can
  change without re-collection. (§6)

## Deferred / Open Questions

### From 2026-07-29 review

- **Should Phase 0 hard-gate all further engineering?** (product-lens, P1) The
  roadmap's own ranking says Phase 0 has "the best ratio of information gained
  to effort spent" and the Recorder call "could obsolete the whole portal
  path" — yet its boxes sat unchecked while Phases 1–3 received a full day of
  engineering. The review recommends making Phase 0 the only committed work
  until its items are done; that is a strategy commitment the review did not
  impose.
- **Right-size the metrics/floor/ledger layer?** (scope-guardian, P2)
  `collection_metrics.py` + `check_collection_floor.py` + the agent-cost and
  health ledgers are ports from a project with different economics (a real
  accuracy metric, multiple operators). Collapsing them deletes working code
  that also guards the pipeline, so the call is contested and deferred.
