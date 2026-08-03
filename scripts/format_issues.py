#!/usr/bin/env python3
"""format_issues.py — render `gh issue list --json` into a bounded prompt block.

Reads the JSON on stdin, writes markdown on stdout. Exists as a Python file
rather than a jq one-liner for two reasons: the truncation arithmetic below is
load-bearing and deserves tests, and a malformed payload must degrade to "none"
rather than injecting a shell error into the architect's prompt.

Bounding matters. An issue body is written by a human with no length budget, and
the architect prompt already carries the nightly review, the full ROADMAP, the
dismissed-findings ledger and the rejection feedback. One pasted stack trace or
log dump could crowd out the context the triage actually runs on. Bodies are
truncated per-issue and the block is capped overall; whatever is cut is
ANNOUNCED, so the architect knows to read the issue itself rather than assuming
it saw all of it.
"""

import json
import sys

# Per-issue body budget. Roughly a screen of prose — enough for a finding with a
# table and a code block, which is the shape the issues in this repo take.
BODY_CHARS = 2500

# Whole-block ceiling, and the number of issues considered. Both are backstops
# against a day when the tracker has fifty issues on it: better to hand the
# architect ten issues it can actually weigh than fifty it skims.
BLOCK_CHARS = 20000
MAX_ISSUES = 10


def _truncate(text, limit):
    """Cut to `limit`, saying so. A silent cut reads as a complete issue body."""
    text = (text or "").strip()
    if len(text) <= limit:
        return text or "_(no body)_"
    return text[:limit].rstrip() + f"\n\n_[body truncated at {limit} chars — read the issue for the rest]_"


def format_issues(issues):
    """Render a list of `gh issue list --json` dicts as markdown."""
    if not issues:
        return "none"

    dropped = max(0, len(issues) - MAX_ISSUES)
    out, used = [], 0
    for it in issues[:MAX_ISSUES]:
        labels = ", ".join(lab.get("name", "") for lab in (it.get("labels") or []))
        header = f"### #{it.get('number', '?')} — {(it.get('title') or '(untitled)').strip()}"
        meta = f"_updated {it.get('updatedAt', 'unknown')}_" + (f" · labels: {labels}" if labels else "")
        block = f"{header}\n{meta}\n\n{_truncate(it.get('body'), BODY_CHARS)}\n"

        # Stop at the ceiling rather than emitting a half block, and count the
        # rest as dropped so the total below stays honest.
        if used + len(block) > BLOCK_CHARS:
            dropped += len(issues[:MAX_ISSUES]) - len(out)
            break
        out.append(block)
        used += len(block)

    if not out:
        return "none"
    if dropped:
        out.append(f"\n_[{dropped} further open issue(s) not shown — run `gh issue list` to see them all]_")
    return "\n".join(out)


def main():
    raw = sys.stdin.read()
    try:
        issues = json.loads(raw) if raw.strip() else []
    except json.JSONDecodeError:
        # The caller logs the raw payload. Emitting "none" keeps a bad response
        # from becoming prompt text the architect might try to triage.
        print("none")
        return 0
    if not isinstance(issues, list):
        print("none")
        return 0
    print(format_issues(issues))
    return 0


if __name__ == "__main__":
    sys.exit(main())
