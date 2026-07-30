#!/usr/bin/env python3
"""Validate spec file boundaries before and after implementation.

A spec's `## Files` section is a contract: the implementer may modify exactly
those files and nothing else. This script enforces it mechanically at two points,
so the judgment call in post-review is about correctness rather than about
whether the diff wandered.

Usage:
  validate_spec_boundaries.py plan <spec1.md> [<spec2.md> ...]
    Emit the subset of specs SAFE to dispatch this cycle, one path per line on
    stdout. CLOSED and QUEUED specs are skipped; among ready specs that touch the
    same file, the first (input order) wins and the rest are deferred to a later
    cycle. Diagnostics go to stderr. Exit 0 normally, 1 only on a hard error.
    This is what the pipeline drives off — one QUEUED spec must never block the
    whole batch, since each spec gets its own branch.

  validate_spec_boundaries.py check-conflicts <spec1.md> [<spec2.md> ...]
    Manual gate: exit non-zero if ANY spec is QUEUED or two specs touch the same
    file. Reports, does not sequence. Answers "are these all ready together?"

  validate_spec_boundaries.py validate-diff <spec.md> <ref>
    Verify the diff main...<ref> touches only the spec's declared files.
    Exit 0 in-bounds, 1 out-of-bounds or if <ref> is unresolvable.
"""

import os
import re
import subprocess
import sys

# Lines under `## Files` that introduce an EXCLUSION list ("Do NOT touch:",
# "Out of scope:"). Once one appears, later bullets are not allow-listed —
# otherwise a spec's own "do not edit these" list becomes permission to edit them.
_EXCLUSION_MARKER = re.compile(
    r"(?i)(do\s*not|don't|must\s*not|not\s+touch|out[-\s]of[-\s]scope|"
    r"not\s+in\s+scope|excluded|off[-\s]limits)"
)
# A file bullet under `## Files`. Two accepted forms:
#   - `path/to/file.py`
#   - `path/to/file.py` — description   (trailing prose ignored)
# The backtick-delimited path wins. A bare token is accepted only when it looks
# path-like, so prose bullets are not mistaken for filenames.
_BULLET_BACKTICK = re.compile(r"^\s*-\s+`([^`\n]+)`")
_BULLET_BARE = re.compile(r"^\s*-\s+([^\s`][^\s]*)")

# Files the pipeline itself writes on every run. They appear in a branch diff
# through no fault of the implementer, so they never count as a boundary breach.
BENIGN_AUTOCOMMIT = {
    "reviews/agent-costs.jsonl",
    "reviews/pipeline-health.log",
    "reviews/spec-attempts.json",
}

# Paths no spec may declare and no executor diff may touch — the rules the
# pipeline is governed BY, and the pipeline itself. Without this list the
# boundary check is circular: the architect agent writes the specs, so a spec
# declaring AI_CONTEXT.md or this file would pass its own boundary validation,
# auto-merge, and push. Self-modification of the guardrails is a human-only
# change (2026-07-29 review, security-lens).
PROTECTED_FILES = {
    "AI_CONTEXT.md",
    "SPEC_TEMPLATE.md",
    "MVP.md",
    "ROADMAP.md",
    "Makefile",
    ".pre-commit-config.yaml",
    ".gitignore",
}
PROTECTED_PREFIXES = (
    "scripts/",
    ".claude/",
    ".github/",
)


def protected(path):
    """True when a path is off-limits to the spec pipeline."""
    # removeprefix, not lstrip: lstrip("./") strips CHARACTERS and would eat
    # the leading dot off ".claude/", unprotecting the whole directory.
    p = path.strip()
    while p.startswith("./"):
        p = p.removeprefix("./")
    return p in PROTECTED_FILES or p.startswith(PROTECTED_PREFIXES)


def _bullet_path(line):
    """Return (file_path, prefix) for a `## Files` bullet, else (None, line).

    `prefix` is the text before the matched path, used to tell an exclusion
    bullet ("- Out of scope: `x.py`") from an in-scope file that merely mentions
    a prohibition ("- `x.py` — do not reformat").
    """
    m = _BULLET_BACKTICK.match(line)
    if m:
        return m.group(1).strip(), line[: m.start(1)]
    m = _BULLET_BARE.match(line)
    if m:
        token = m.group(1).strip("`").strip()
        if "/" in token or "." in token:
            return token, line[: m.start(1)]
    return None, line


def extract_files_from_spec(spec_path):
    """Extract a spec's declared files.

    Returns (files_list, has_queue_status, is_closed); files_list is None on a
    read error.
    """
    if not os.path.exists(spec_path):
        print(f"ERROR: Spec file not found: {spec_path}", file=sys.stderr)
        return None, None, None

    with open(spec_path, encoding="utf-8") as f:
        content = f.read()

    # Tolerate the colon inside or outside the bold markers.
    has_queue = bool(re.search(r"^\*\*Status\*?\*?:?\*?\*?\s*QUEUED", content, re.MULTILINE))

    # A spec whose status begins with a terminal keyword is finished. The
    # dispatcher selects specs by NAME out of the decisions file and does not read
    # this header, so without this check a completed spec sitting at repo root can
    # be re-dispatched forever. The keyword must immediately follow the marker, so
    # "Ready for implementation" never matches IMPLEMENTED.
    is_closed = bool(
        re.search(
            r"^\*\*Status\*?\*?:?\*?\*?\s*"
            r"(DONE|ARCHIVED|MERGED?|COMPLETED?|IMPLEMENTED|RETIRED)\b",
            content,
            re.MULTILINE | re.IGNORECASE,
        )
    )

    # Tolerate a titled heading ("## Files (in scope)"); stop at the next heading.
    section = re.search(r"^##\s+Files\b[^\n]*\n(.*?)(?=^##\s|\Z)", content, re.DOTALL | re.IGNORECASE | re.MULTILINE)
    if not section:
        print(f"WARNING: No '## Files' section found in {spec_path}", file=sys.stderr)
        return [], has_queue, is_closed

    files = []
    for line in section.group(1).splitlines():
        path, prefix = _bullet_path(line)
        if path is None:
            # A standalone exclusion line ("- Do NOT touch:") ends collection.
            if _EXCLUSION_MARKER.search(line):
                break
            continue
        if _EXCLUSION_MARKER.search(prefix):
            break
        files.append(path)

    return sorted(files), has_queue, is_closed


def _load_specs(spec_paths):
    loaded = []
    for spec_path in spec_paths:
        files, has_queue, is_closed = extract_files_from_spec(spec_path)
        if files is None:
            return None
        loaded.append((spec_path, files, has_queue, is_closed))
    return loaded


def plan_cmd(spec_paths):
    loaded = _load_specs(spec_paths)
    if loaded is None:
        return 1

    claimed = {}  # file -> spec that claimed it
    dispatch = []
    for spec_path, files, has_queue, is_closed in loaded:
        name = os.path.basename(spec_path)
        if is_closed:
            print(f"SKIP (CLOSED): {name}", file=sys.stderr)
            continue
        if has_queue:
            print(f"SKIP (QUEUED): {name}", file=sys.stderr)
            continue
        if not files:
            # Previously a warning that still dispatched. An undeclared boundary
            # means an unbounded diff, which defeats the entire enforcement
            # layer — so it now blocks (2026-07-29 review, security-lens).
            print(f"SKIP (NO ## Files): {name} — no boundary declared; not dispatching", file=sys.stderr)
            continue
        bad = [f for f in files if protected(f)]
        if bad:
            print(f"SKIP (PROTECTED): {name} declares protected path(s): {', '.join(bad)}", file=sys.stderr)
            continue
        conflict = next((f for f in files if f in claimed), None)
        if conflict:
            print(f"DEFER: {name} (file '{conflict}' already claimed by {claimed[conflict]})", file=sys.stderr)
            continue
        for f in files:
            claimed[f] = name
        dispatch.append(spec_path)

    for spec_path in dispatch:
        print(spec_path)
    return 0


def check_conflicts_cmd(spec_paths):
    loaded = _load_specs(spec_paths)
    if loaded is None:
        return 1

    queued = [os.path.basename(p) for p, _, q, _c in loaded if q]
    if queued:
        print("ERROR: These specs are marked QUEUED and must not be dispatched:")
        for spec in queued:
            print(f"  - {spec}")
        return 1

    file_to_specs = {}
    for spec_path, files, _, _c in loaded:
        for file in files:
            file_to_specs.setdefault(file, []).append(os.path.basename(spec_path))

    conflicts = {f: s for f, s in file_to_specs.items() if len(s) > 1}
    if conflicts:
        print("ERROR: File contention detected between specs:")
        for file, specs in sorted(conflicts.items()):
            print(f"  - {file}: {', '.join(specs)}")
        print("\nSequence these (mark the later one QUEUED) rather than dispatching together.")
        return 1

    print("OK: No conflicts or QUEUED specs detected")
    return 0


def validate_diff_cmd(spec_path, ref):
    files_declared, _, _ = extract_files_from_spec(spec_path)
    if files_declared is None:
        return 1

    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    try:
        result = subprocess.run(
            ["git", "diff", "--name-only", f"main...{ref}"], cwd=repo_root, capture_output=True, text=True, timeout=10
        )
    except subprocess.TimeoutExpired:
        print("ERROR: git diff timed out", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"ERROR: Failed to get diff: {e}", file=sys.stderr)
        return 1

    if result.returncode != 0:
        print(
            f"ERROR: `git diff main...{ref}` failed (is '{ref}' a valid ref?): {result.stderr.strip()}", file=sys.stderr
        )
        return 1

    changed = set(result.stdout.strip().split("\n")) if result.stdout.strip() else set()
    changed -= BENIGN_AUTOCOMMIT

    # Protected paths fail the diff even when the spec declared them — the
    # declaration itself is invalid, and `plan` would have refused it. This is
    # the backstop for a spec dispatched around the planner.
    touched_protected = sorted(f for f in changed if protected(f))
    if touched_protected:
        print("ERROR: Diff touches protected path(s) the pipeline may never modify:")
        for f in touched_protected:
            print(f"  - {f}")
        print("These are the pipeline's own guardrails; changing them is human-only.")
        return 1

    out_of_bounds = changed - set(files_declared)
    if out_of_bounds:
        print("ERROR: Diff contains files not in the spec's '## Files' list:")
        for file in sorted(out_of_bounds):
            print(f"  - {file}")
        return 1

    untouched = set(files_declared) - changed
    if untouched:
        print(f"INFO: Spec declares but does not modify: {', '.join(sorted(untouched))}")

    print(f"OK: Diff is in-bounds (modified: {', '.join(sorted(changed)) or 'none'})")
    return 0


def main():
    if len(sys.argv) < 2:
        print(__doc__, file=sys.stderr)
        return 1

    cmd = sys.argv[1]
    if cmd == "plan":
        if len(sys.argv) < 3:
            print("Usage: validate_spec_boundaries.py plan <spec1.md> [...]", file=sys.stderr)
            return 1
        return plan_cmd(sys.argv[2:])
    if cmd == "check-conflicts":
        if len(sys.argv) < 3:
            print("Usage: validate_spec_boundaries.py check-conflicts <spec1.md> [...]", file=sys.stderr)
            return 1
        return check_conflicts_cmd(sys.argv[2:])
    if cmd == "validate-diff":
        if len(sys.argv) < 4:
            print("Usage: validate_spec_boundaries.py validate-diff <spec.md> <ref>", file=sys.stderr)
            return 1
        return validate_diff_cmd(sys.argv[2], sys.argv[3])

    print(f"ERROR: Unknown command: {cmd}", file=sys.stderr)
    print(__doc__, file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
