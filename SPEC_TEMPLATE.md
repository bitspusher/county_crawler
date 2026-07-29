# Spec Template

**CRITICAL: file boundaries are strictly enforced by post-review. Violations are
an automatic REJECT.**

This is the structure for a `GEMINI_*.md` implementation spec. The architect
writes these; the implementer executes them; post-review checks the diff against
the `## Files` list mechanically before a human-equivalent judgment is applied.

---

## Status line

Include an explicit status line at the top. Valid values:

- `**Status:** Ready for implementation` — dispatch immediately.
- `**Status:** QUEUED — dispatch only AFTER <other-spec> merges` — blocks
  dispatch until the dependency lands. Use this whenever two specs edit the same
  file; dispatching both concurrently makes them contend.
- `**Status:** DONE` — all tasks merged, but follow-ups may remain. Stays at repo
  root.
- `**Status:** ARCHIVED` — fully complete, nothing outstanding.
  `scripts/cleanup.sh` files these into `docs/archived/` with a reversible
  `git mv`. Only the architect sets this, and only when certain.

Example:

```markdown
**Status:** QUEUED — dispatch only AFTER `GEMINI_RUN_LOG_NOTES.md` merges.
Both specs edit `collect_sjc.py`; dispatching them concurrently would contend on
the same file. The next architect flips this line to "Ready for implementation"
once the run_log change is on main.
```

## `## Files` — REQUIRED

List **exactly** the files the implementer may modify. This is not optional and
it is not advisory.

```markdown
## Files
The ONLY files in scope for this spec are:
- `collect_sjc.py`
- `tests/test_storage.py`

Any diff that modifies a file not in the two-item list above is an automatic
REJECT. Do not edit MVP.md, ROADMAP.md, AI_CONTEXT.md, or any other file.
```

Rules:

1. List only files that genuinely need modification.
2. The implementer will not modify any file outside the list. It may **read**
   anything.
3. If a file is not in the list, do not mention it in the spec — it is out of
   scope.
4. If pre-commit reformats a file outside the list, revert that file before
   committing.

## Workflow

1. **Architect writes the spec** with an explicit `## Files` section.
2. **Pre-dispatch planning** (`scripts/validate_spec_boundaries.py plan`) drops
   QUEUED and closed specs and defers same-file contenders to a later cycle.
3. **Implementer works** on its own branch, with the file list in its prompt.
4. **Post-implementation validation**
   (`scripts/validate_spec_boundaries.py validate-diff`) checks the diff touches
   only declared files.
5. **Architect post-review** confirms the implementation matches the spec and
   decides `VERDICT: MERGE` or `VERDICT: REJECT`.

## What makes a good spec here

- **Narrow.** This repo is ~800 lines with no ground truth to catch a mistake
  (see `AI_CONTEXT.md` rule 10). A 20-line change with a test beats a 200-line
  improvement.
- **Specific files**, named explicitly.
- **The what and the why**, not the how. Say which invariant must hold; let the
  implementer choose the code.
- **A testing strategy** that does not require the network.
- **Acceptance criteria** that include "no files other than those listed are
  modified".
- **Sequencing** — mark QUEUED if it shares a file with another pending spec.

## Standing rules for this project

These apply to every spec. Restate the relevant ones in the spec body.

**No live network in tests.** A new test that reaches the Tyler Eagle portal is
marked `live` and stays out of the default run. Drive `Portal` through a fake page
instead — `tests/test_portal_search.py` shows the pattern.

**CLI flags must be tested through the real entry point.** If a spec adds or
changes a flag, subcommand, or alternate code path on `collect_sjc.py` or a
`scripts/*.py`, the acceptance criteria must include a test that exercises it
through the actual `main()`, not just the helper it calls. A helper test does not
prove the flag is wired to it.

**Failure paths need tests, not just happy paths.** This project's characteristic
bug is data that looks fine and is wrong. If a spec touches a path where a
failure could be stored as a result — a zero-row window, a capped window, a
failed detail fetch — the test for the failure branch is mandatory, not optional.

**Never weaken a test to make a diff pass.** Widening a threshold, loosening an
assertion, deleting a case, or marking something xfail to get green is an
automatic REJECT. Fix forward.

**Do not touch the derivation assumptions casually.** `DTT_RATE_PER_1000` and the
`sale_class` CASE expression encode unvalidated assumptions (§6.4, §6.5) that
only live data can settle. A spec may restructure how they are applied; it may
not silently change what they assert.

## Template

```markdown
# <SHORT TITLE>

**Status:** Ready for implementation

## Context
Why this matters, with a § reference into MVP.md or a ROADMAP Gap number.

## Files
The ONLY files in scope for this spec are:
- `path/to/file.py`
- `tests/test_file.py`

Any diff modifying a file not listed above is an automatic REJECT.

## Requirements
1. ...
2. ...

## Out of scope
Explicitly, what not to change while in here.

## Testing
- Which tests to add, in which file, asserting what.
- The command that verifies it: `pytest -m "not live" -q`

## Acceptance criteria
- [ ] ...
- [ ] `make check` passes.
- [ ] No files other than those listed in `## Files` are modified.
```
