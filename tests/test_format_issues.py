"""Tests for the GitHub-issue prompt block.

The pipeline feeds open issues into the architect's triage. Everything here
guards the same property: what the architect sees must either be the issue, or
say plainly that it is not the whole issue. A silently truncated or silently
dropped issue is a finding the architect will believe it has fully weighed.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from format_issues import BLOCK_CHARS, BODY_CHARS, MAX_ISSUES, format_issues

pytestmark = pytest.mark.unit

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "format_issues.py"


def _issue(number=1, title="A finding", body="Something is wrong.", labels=None, updated="2026-08-01T09:00:00Z"):
    return {
        "number": number,
        "title": title,
        "body": body,
        "labels": labels or [],
        "updatedAt": updated,
    }


def _run(payload):
    """Drive the script the way architect-review.sh does: JSON on stdin."""
    return subprocess.run(
        [sys.executable, str(SCRIPT)],
        input=payload,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


# ── the normal case ──────────────────────────────────────────────────────


def test_renders_number_title_and_body():
    out = format_issues([_issue(number=7, title="Pagination can skip", body="Doc 2026-057938 returned twice.")])
    assert "#7" in out
    assert "Pagination can skip" in out
    assert "2026-057938" in out


def test_labels_are_included_when_present():
    out = format_issues([_issue(labels=[{"name": "bug"}, {"name": "P1"}])])
    assert "bug, P1" in out


def test_no_labels_does_not_leave_a_dangling_separator():
    assert "·" not in format_issues([_issue(labels=[])])


def test_several_issues_all_appear():
    out = format_issues([_issue(number=n, title=f"Finding {n}") for n in (1, 2, 3)])
    for n in (1, 2, 3):
        assert f"#{n}" in out


# ── absence ──────────────────────────────────────────────────────────────


def test_no_open_issues_renders_as_none():
    """The prompt block says "none", which the architect reads as "nothing
    filed" — not as a missing section it should wonder about."""
    assert format_issues([]) == "none"


def test_an_empty_body_is_marked_rather_than_blank():
    """A blank stretch under a heading reads as a rendering failure."""
    assert "_(no body)_" in format_issues([_issue(body="")])


def test_a_null_body_is_handled():
    assert "_(no body)_" in format_issues([_issue(body=None)])


# ── bounding, announced ──────────────────────────────────────────────────


def test_a_long_body_is_truncated_and_says_so():
    """Silent truncation is the failure mode this whole module exists to avoid:
    the architect would triage half an issue believing it saw all of it."""
    out = format_issues([_issue(body="x" * (BODY_CHARS * 3))])
    assert "body truncated" in out
    assert len(out) < BODY_CHARS * 2


def test_a_body_at_the_limit_is_not_truncated():
    """Off-by-one guard: exactly BODY_CHARS must pass through untouched."""
    assert "truncated" not in format_issues([_issue(body="x" * BODY_CHARS)])


def test_more_issues_than_the_cap_are_counted_not_dropped_silently():
    out = format_issues([_issue(number=n) for n in range(MAX_ISSUES + 4)])
    assert "4 further open issue(s) not shown" in out


def test_the_block_ceiling_holds_and_reports_what_it_cut():
    """Ten maximum-length bodies would blow the prompt budget on their own."""
    out = format_issues([_issue(number=n, body="x" * BODY_CHARS) for n in range(MAX_ISSUES)])
    assert len(out) <= BLOCK_CHARS + 500  # the ceiling, plus the closing notice
    assert "not shown" in out


def test_the_ceiling_never_emits_a_half_block():
    """A body cut mid-sentence with no notice would read as the whole issue."""
    out = format_issues([_issue(number=n, body="x" * BODY_CHARS) for n in range(MAX_ISSUES)])
    assert out.count("###") == out.count("_updated ")


# ── malformed input degrades to "none", never to a broken prompt ─────────


def test_invalid_json_becomes_none():
    """gh can fail in ways that still exit 0 with junk on stdout. That must not
    become prompt text the architect tries to triage."""
    assert _run("not json at all") == "none"


def test_empty_stdin_becomes_none():
    assert _run("") == "none"


def test_a_json_object_instead_of_a_list_becomes_none():
    """gh's error shape is an object, not a list."""
    assert _run('{"message": "Not Found"}') == "none"


def test_the_script_round_trips_a_real_payload():
    payload = json.dumps([_issue(number=1, title="Findings 7/30", body="## Three findings")])
    out = _run(payload)
    assert "#1" in out
    assert "Findings 7/30" in out
