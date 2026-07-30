# AI_CONTEXT.md — hard rules for automated contributors

Read this before writing code in this repo. Every rule here is load-bearing and
derived from [MVP.md](MVP.md); the section references (§) point there. The
agent pipeline (`scripts/nightly-review.sh` and friends) treats a violation of
any rule below as grounds to reject a diff outright, regardless of whether the
tests pass.

These are constraints, not preferences. If a spec appears to require breaking
one, the spec is wrong — say so instead of implementing it.

---

## 1. Never store a derived value

Sale price and sale class are computed in `VIEWS`, never written to a table.
The documentary-transfer-tax rate (§6.4) and the §11926 zero-tax split (§6.5)
are both **unvalidated assumptions**. Keeping them in views means revising them
is a view rebuild, not a re-collection of the whole county.

- `DTT_RATE_PER_1000` may only be read inside the view SQL.
- Do not add a `price` or `sale_class` column to `detail_obs` or `index_obs`.
- Do not "cache" a derivation for speed. The dataset is ~30 rows/month (§7.3).

## 2. The database is append-only

Every fetch is an observation stamped with `observed_at`. Nothing is updated in
place, nothing is deleted.

- No `UPDATE` or `DELETE` against `index_obs`, `detail_obs`, or `party_obs`.
- `INSERT` only. Read through `v_latest_index` / `v_latest_detail` to get the
  most recent observation per document.
- A re-collection of an already-collected window is expected and must produce
  new rows, not a conflict.

## 3. Zero rows where rows were expected is a failure, never a result

The portal returns an empty grid for both "no documents matched" and "your
session is no longer authenticated." Treating those the same silently poisons
the dataset with absence.

- An unexpected zero-row window aborts the run with a non-zero exit.
- `--allow-zero-rows` is the only escape hatch, and it must be passed
  deliberately by a human who has checked why.
- Never let a caught exception fall through into an empty-result code path.

## 4. A result cap is an error, not an empty page

`Portal.search` raises `ResultCapExceeded` when the server reports more
documents than it will return (§5.1). That window's data is *incomplete*, which
is worse than missing, because it looks complete.

- Never catch `ResultCapExceeded` and continue as though the window returned
  nothing.
- Never widen the window to "get more"; narrow it.

## 5. Deterministic parsers only — no LLM at runtime

`parse_results` and `parse_detail` are regex/line-based on purpose (§6.2).
A deterministic parser fails loudly on a portal change; a model fails silently
and a single misread digit quietly corrupts a price.

- No model calls anywhere in the collection or parsing path.
- Parse the rendered label/value **text lines**, not the markup structure —
  Eagle's jQuery-Mobile div soup changes between releases, the text does not.

## 6. Stay polite, and never defeat the CAPTCHA

The portal is a county server behind a reCAPTCHA and a disclaimer (§10).

- Requests stay serial with a sleep between each (`Portal._pause`, `--delay`).
  Never parallelise, never remove the delay, never add a retry storm.
- The CAPTCHA is solved **manually, once per profile**, and the clearance
  persists in `./.browser_profile`. Do not add solver services, bypass attempts,
  or automated clicking of the disclaimer.
- Never fetch from `/Web/cart` or any paid-image path. Index and detail
  metadata only.
- Short windows (default 3 days) exist to stay under the result cap — the
  server ignores the doc-type filter, so every sweep is unfiltered and a full
  month is ~130 pages, which always trips the cap. Do not batch windows wider.

## 7. Do not collect `Substitution Of Trustee`

1016 documents/month, and it tracks loan *payoffs*, not distress — servicers
substitute a trustee immediately before reconveying a paid-off loan (§3). Adding
it would swamp the product in false positives. `DOCTYPES` holds NOTS (41) and
TDUS (22); rescissions and cancellations are the only approved additions
(ROADMAP Phase 3).

## 8. Never state a property identifier the data does not contain

The recorder index carries **no address and no APN** (§6.3). Any output that
implies otherwise is a correctness bug.

- Unavailable fields are printed as unavailable, not omitted (§7).
- The grantor/grantee index is a finding aid, not title work — carry that
  caveat into user-facing output.
- Do not infer an address from a party name and present it as fact.

## 9. Tests must not hit the network by default

The `live` pytest marker is excluded from the default run (`pyproject.toml`).

- A new test that reaches the portal is marked `live`, always.
- Never mark a live test `unit` or `integration` to "get it running in CI".
- A missing committed fixture is a **failure**, not a skip — see
  `tests/conftest.py`. Silent skips report green while covering nothing.

## 10. Scope discipline

`ROADMAP.md` is the canonical priority ordering, and its "Deliberately parked"
and "Named mistakes not to make" sections are binding.

- Do not implement parked items (`Default` collection, scoring/ranking,
  prediction, tax-default collection, multi-county, homeowner-facing anything).
- Do not start Phase N+1 while Phase N is unmerged.
- Prefer the smallest correct change. This is a small repo with almost no
  ground truth to catch a mistake; ambitious rewrites are strictly worse than
  narrow fixes.
- Roadmap items tagged `[human]` need a headed browser, a phone call, or the
  founder's judgment. Never write a spec for one.

## 11. Individual party names stay out of tracked files

The collected records name private individuals in active foreclosure. Public
record or not, those names do not belong in git history — the repo is private
today, but history makes every future visibility decision retroactive.

- Party names of **individuals** appear only in the local database (`sjc.db`)
  and in report/CSV output — never in tracked files: docs, code comments, test
  fixtures, captured fixtures, review artifacts, commit messages.
- Reference documents by **document number** instead. Company names (trustee
  firms, banks, LLCs) may appear where they carry analytical content.
- Captured fixtures (rule 9's capture flow) are redacted before commit.
- Pipeline review artifacts quote diffs and agent output; a diff touching test
  fixtures or examples must not carry individual names into `reviews/`.
