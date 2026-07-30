# Development

Test suite, tooling, and the agent pipeline. Read [AI_CONTEXT.md](AI_CONTEXT.md)
first — it holds the hard rules, and a change that breaks one is rejected even if
the tests pass.

---

## Setup

```sh
python -m venv .venv && source .venv/bin/activate
pip install -e '.[dev]'
python -m playwright install chromium     # only needed to actually collect
pre-commit install
```

Playwright is **not** required to run the test suite. `collect_sjc.py` imports it
softly and only `main()` insists on it, so the parsers, the date-window logic and
the derivation views are all testable — and lintable, and runnable from cron —
with no browser installed.

## Tests

```sh
make test          # default: everything except `live`
make test-unit     # the fast pure-logic subset
make test-live     # opt-in, hits the county's server — see below
make check         # CI-style gate: ruff + format + mypy + tests
make metrics       # deterministic project-health numbers
```

Three markers, defined in `pyproject.toml`:

| Marker | What it means |
|---|---|
| `unit` | Pure logic. Parsers, date windows, derivation views over an in-memory DB. No I/O. |
| `integration` | Touches the filesystem or a real sqlite file. Never the network. |
| `live` | Hits the live Tyler Eagle portal. Needs a CAPTCHA-cleared `./.browser_profile` and a human. **Excluded by default.** |

`live` is excluded for the same reason MVP.md §10 keeps requests serial: every
live test is a real request to a county server behind a reCAPTCHA. A test run
must never become a crawl. If you add a test that reaches the portal, mark it
`live` — always.

### What the suite does and does not establish

**Read `tests/fixtures/README.md` before trusting a green run.** All five markup
fixtures are currently hand-authored, not captured from the portal. So:

- The parser tests **do** lock in current behaviour and cover edge cases
  (multi-party rows, `$0.00` tax, missing detail links, the cap message, the
  disclaimer gate).
- They **do not** prove the parsers handle San Joaquin County's real markup. The
  fixtures were written by reading the parsers, so their agreement is partly
  circular.

`scripts/capture_fixtures.py --headed` replaces them with live markup and
rewrites the provenance table. **Expect parser tests to fail on the first real
capture** — that failure is the point, and it is the first genuine signal this
project has had about whether the parsers work.

The view tests are on firmer ground: `v_auction_sales`, `v_upcoming` and
`v_repeat_buyers` are exercised against a real in-memory schema. They verify the
derivations are implemented **as specified** — not that the specification is
right. Whether $1.10/$1,000 holds for Stockton, and whether $0.00 tax really
identifies a credit bid, are open questions only live data can answer
(ROADMAP Phase 2).

### The regression gate

```sh
python3 scripts/check_collection_floor.py          # gate
python3 scripts/check_collection_floor.py --update # accept current state as floor
```

The floor lives in `reviews/collection-floor.json` and ratchets upward. It fails
on:

1. **A falling test count.** The most valuable check here — deleting or
   xfail-ing a test is the cheapest way to turn red green, and a summary line
   cannot tell "fixed" from "removed the test".
2. **Captured fixtures reverting to synthetic.**
3. **The default suite not passing.**
4. **A falling observation count** — the tables are append-only, so a drop means
   history was deleted or rebuilt.
5. **New capped windows.**

Use `--update` only when a reduction is deliberate.

---

## The agent pipeline

Four phases, chained sequentially, ported from the chess_game_annotator setup and
adapted to this project. Only Phase 1 needs a cron entry; each phase triggers the
next, and the cron fallbacks exist for when chaining breaks.

| Phase | Script | What it does |
|---|---|---|
| 1 | `nightly-review.sh` | Runs three agents in parallel: `test-quality-auditor`, `product-manager`, `data-integrity-auditor`. Feeds them real computed metrics, not estimates. |
| 2 | `architect-review.sh` | `software-architect` triages the findings, writes `GEMINI_*.md` specs, commits so Phase 3 starts clean. |
| 3 | `implement.sh` | The executor implements each dispatchable spec on its own branch `auto/<slot>-<spec>`. Validates boundaries, runs tests, writes a report. Does not merge. |
| 4 | `architect-post-review.sh` | `software-architect` decides `VERDICT: MERGE` or `REJECT` per branch, merges, then runs the regression gate. |

Plus two off-chain jobs: `metrics-snapshot.sh` (daily) and
`compliance-review.sh` (weekly, report-only — it covers the ROADMAP Phase 0
items that are phone calls and records requests rather than code).

### Agents

`.claude/agents/`, each with `memory: project`:

| Agent | Model | Role |
|---|---|---|
| `software-architect` | opus | Triage, specs, post-review verdicts. Runs in Phases 2 and 4. |
| `data-integrity-auditor` | opus | Ways the dataset can be silently wrong. Every finding must come with the cheapest check that would kill it. |
| `test-quality-auditor` | sonnet | Coverage gaps, tautologies, silent skips. |
| `product-manager` | sonnet | Priorities, scope, the untested hypotheses. Biased toward cutting. |
| `legal-compliance` | sonnet | Portal terms, CPRA/SB 272 routes, representation limits. Report-only. |

`data-integrity-auditor` replaces the chess project's `chief-science-officer`:
same shape (propose, don't merely observe) pointed at this project's actual hard
problem, which is data that looks correct and is not.

### Safety machinery

The parts that stop the pipeline hurting the repo:

- **File boundaries.** Every spec declares a `## Files` list.
  `validate_spec_boundaries.py` enforces it before dispatch (`plan`) and after
  (`validate-diff`). An exclusion list ("Do NOT touch: …") never becomes an
  allow-list — there is a test for that.
- **Protected paths.** A hard deny-list (`AI_CONTEXT.md`, `SPEC_TEMPLATE.md`,
  `MVP.md`, `ROADMAP.md`, `Makefile`, `scripts/`, `.claude/`, …) that no spec
  may declare and no executor diff may touch — otherwise the boundary check is
  circular, since the architect agent writes the specs. A spec with no
  `## Files` section is refused outright, not warned through. Self-modification
  of the guardrails is human-only.
- **`[human]` roadmap tags.** Items needing a headed browser, a phone call, or
  founder judgment are tagged in ROADMAP.md; the architect must not spec them.
- **A boundary violation overrides a MERGE verdict.** Boundary enforcement is
  mechanical, so an agent talking itself past one is exactly the failure the
  check exists to prevent.
- **Three-way outcomes.** `review_outcome.py` classifies MERGE / REJECT /
  **INFRA**. INFRA means the reviewer never formed a verdict (quota, auth, empty
  response); it preserves the branch and does **not** count toward the reject
  threshold, so an API outage cannot block a good spec.
- **Circuit breaker.** Three consecutive rejects block a spec until a human
  re-scopes it. `reviews/needs-human-review.md` collects escalations.
- **No-output refund.** An executor that produced no diff has its attempt
  refunded — that says nothing about the spec's achievability.
- **flock.** `.pipeline.lock` serializes phases within this project.
- **Push is gated.** `main` is pushed only when the regression gate is green, so
  a regression stays local.
- **Timeouts are spec-local.** The executor often commits working code and then
  hangs; a timeout falls through to the normal verify path rather than
  discarding the work and skipping the rest of the batch.

### Running it by hand

```sh
export PIPELINE_ID=2026-07-29-0600        # pin a slot; omit to use the wall clock
bash scripts/nightly-review.sh            # runs the whole chain
bash scripts/architect-review.sh          # or one phase at a time
```

Artifacts land in `reviews/<PIPELINE_ID>-*.md`. The durable ledgers —
`pipeline-health.log`, `spec-attempts.json`, `collection-metrics.jsonl`,
`collection-floor.json`, `agent-costs.jsonl`, `dismissed-findings.md`,
`needs-human-review.md` — are tracked in git deliberately: they are how a later
cycle knows what already happened. Per-run logs are gitignored.

### Scheduling it

⚠️ **Do not run `crontab scripts/crontab.txt`.** That replaces the entire
crontab, and this machine already runs the chess_game_annotator pipeline out of
it. `scripts/crontab.txt` is a fragment to **append**:

```sh
crontab -l > /tmp/cron.bak                      # back up, always
( crontab -l; cat scripts/crontab.txt ) | crontab -
crontab -l                                      # verify BOTH pipelines survived
```

Slots are `00,06,12,18` (6-hour stride), with entries firing at `03,09,15,21` —
offset from the chess pipeline's even hours so the two do not start together and
compete for the same rolling Claude quota. Each project has its own lock file, so
flock will not serialize them for you; the offset is the only thing that does.

Four cycles a day rather than the chess pipeline's twelve, because this repo is
~800 lines with 23 open roadmap items and a human-gated blocker at its centre. At
twelve the roadmap is exhausted in a week and the architect starts inventing
work, which on a codebase with no ground truth is actively harmful. Raise
`STRIDE_HOURS` in `scripts/pipeline_id.sh` once collection is unblocked.

### Before you enable it

The probe question this section originally named was settled 2026-07-29. The
authoritative enable-gate is ROADMAP.md's "Deliberately parked" entry for the
agent pipeline: (a) Phase 0 done, (b) `[human]`-tag enforcement beyond
prompt-only, (c) at least 6 open `[pipeline]`-tagged roadmap items across two
or more phases. All three must hold before the crontab is installed.

## Housekeeping

```sh
bash scripts/cleanup.sh            # dry-run
bash scripts/cleanup.sh --apply    # act
```

Narrow by design: scratch files, `debug/` dumps, aged per-cycle artifacts,
architect-`ARCHIVED` specs (moved via `git mv`, never deleted), and empty
`auto/*` branches. It never touches `sjc.db`, `.browser_profile`, or
`tests/fixtures/` — the database is append-only and irreplaceable without
re-requesting from a county server, and a captured fixture needs a human at a
CAPTCHA to reacquire.
