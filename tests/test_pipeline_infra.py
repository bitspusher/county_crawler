"""Tests for the agent pipeline's own support scripts.

These matter more than they look. The boundary validator is the only mechanical
check standing between an executor and the rest of the repo, and the outcome
classifier decides whether a branch is merged, retried, or deleted. A bug in
either is a bug in the thing that is supposed to catch bugs.
"""

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

pytestmark = pytest.mark.unit


def _load(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


vsb = _load("validate_spec_boundaries")
review_outcome = _load("review_outcome")


# ── review_outcome.classify ──────────────────────────────────────────────


def test_merge_verdict_classifies_as_merge():
    assert review_outcome.classify("VERDICT: MERGE\n\nLooks good.") == "MERGE"


def test_reject_verdict_classifies_as_reject():
    assert review_outcome.classify("VERDICT: REJECT\n\nOut of bounds.") == "REJECT"


def test_first_verdict_wins():
    """The agent is told to emit exactly one. If it emits two, take the first
    rather than letting a trailing mention flip a decision."""
    assert review_outcome.classify("VERDICT: REJECT\n...\nVERDICT: MERGE") == "REJECT"


def test_quota_message_classifies_as_infra_not_reject():
    """The distinction that protects the circuit breaker.

    A quota failure recorded as REJECT would, after three outages, block a
    perfectly good spec and escalate it to a human for no reason.
    """
    assert review_outcome.classify("You've hit your session limit. Resets 3pm.") == "INFRA"


def test_empty_agent_output_classifies_as_infra():
    assert review_outcome.classify("[ERROR] Architect agent returned empty output for auto/x.") == "INFRA"


def test_unparseable_review_defaults_to_infra():
    """Silence is evidence of a broken review, not of a bad diff.

    INFRA preserves the branch for a retry; REJECT deletes it. Defaulting the
    other way would discard real work whenever the reviewer misformatted.
    """
    assert review_outcome.classify("I had a look and it seems fine to me.") == "INFRA"


def test_lowercase_verdict_word_is_not_a_verdict():
    assert review_outcome.classify("my verdict: merge, i think") == "INFRA"


# ── extract_files_from_spec ──────────────────────────────────────────────


def _spec(tmp_path, body, name="GEMINI_TEST.md"):
    p = tmp_path / name
    p.write_text(body, encoding="utf-8")
    return str(p)


def test_extracts_backticked_file_bullets(tmp_path):
    s = _spec(
        tmp_path,
        """# T
**Status:** Ready for implementation

## Files
- `collect_sjc.py`
- `tests/test_storage.py`
""",
    )
    files, queued, closed = vsb.extract_files_from_spec(s)
    assert files == ["collect_sjc.py", "tests/test_storage.py"]
    assert queued is False and closed is False


def test_ignores_trailing_description_after_the_path(tmp_path):
    s = _spec(
        tmp_path,
        """## Files
- `collect_sjc.py` — add the run_log writes
""",
    )
    files, _, _ = vsb.extract_files_from_spec(s)
    assert files == ["collect_sjc.py"]


def test_exclusion_list_does_not_become_an_allow_list(tmp_path):
    """The security-critical case.

    A spec that says "do NOT touch AI_CONTEXT.md" must not thereby AUTHORIZE
    editing AI_CONTEXT.md. Collection stops at the exclusion marker.
    """
    s = _spec(
        tmp_path,
        """## Files
- `collect_sjc.py`

Do NOT touch:
- `AI_CONTEXT.md`
- `MVP.md`
""",
    )
    files, _, _ = vsb.extract_files_from_spec(s)
    assert files == ["collect_sjc.py"]
    assert "AI_CONTEXT.md" not in files


def test_inline_exclusion_bullet_is_excluded(tmp_path):
    s = _spec(
        tmp_path,
        """## Files
- `collect_sjc.py`
- Out of scope: `ROADMAP.md`
""",
    )
    files, _, _ = vsb.extract_files_from_spec(s)
    assert files == ["collect_sjc.py"]


def test_a_prohibition_after_the_path_keeps_the_file_in_scope(tmp_path):
    """ "- `x.py` — do not reformat" describes an in-scope file.

    The marker's position decides: before the path it excludes, after it merely
    describes. Getting this backwards would silently shrink every spec's scope.
    """
    s = _spec(
        tmp_path,
        """## Files
- `collect_sjc.py` — do not reformat the parsers
""",
    )
    files, _, _ = vsb.extract_files_from_spec(s)
    assert files == ["collect_sjc.py"]


def test_prose_bullets_are_not_mistaken_for_files(tmp_path):
    s = _spec(
        tmp_path,
        """## Files
- `collect_sjc.py`
- everything else stays untouched
""",
    )
    files, _, _ = vsb.extract_files_from_spec(s)
    assert files == ["collect_sjc.py"]


def test_titled_files_heading_is_tolerated(tmp_path):
    s = _spec(
        tmp_path,
        """## Files (in scope)
- `collect_sjc.py`

## Requirements
- `not_a_file.py` should be ignored
""",
    )
    files, _, _ = vsb.extract_files_from_spec(s)
    assert files == ["collect_sjc.py"]


def test_missing_files_section_yields_empty_list_not_none(tmp_path):
    """Empty means "no boundary declared" and is warned about; None means a read
    error and aborts. They must not be conflated."""
    files, _, _ = vsb.extract_files_from_spec(_spec(tmp_path, "# T\nNo files here.\n"))
    assert files == []


def test_missing_spec_file_returns_none(tmp_path):
    files, _, _ = vsb.extract_files_from_spec(str(tmp_path / "nope.md"))
    assert files is None


@pytest.mark.parametrize(
    "line",
    [
        "**Status:** QUEUED — after the other one",
        "**Status**: QUEUED",
    ],
)
def test_queued_status_detected_in_both_punctuations(tmp_path, line):
    _, queued, _ = vsb.extract_files_from_spec(_spec(tmp_path, f"# T\n{line}\n\n## Files\n- `a.py`\n"))
    assert queued is True


@pytest.mark.parametrize("word", ["DONE", "ARCHIVED", "MERGED", "IMPLEMENTED", "RETIRED"])
def test_terminal_statuses_mark_a_spec_closed(tmp_path, word):
    _, _, closed = vsb.extract_files_from_spec(_spec(tmp_path, f"# T\n**Status:** {word}\n\n## Files\n- `a.py`\n"))
    assert closed is True


def test_ready_for_implementation_is_not_closed(tmp_path):
    """ "Ready" must not match IMPLEMENTED.

    The keyword has to follow the marker immediately; a substring match here
    would make every ready spec undispatchable.
    """
    _, _, closed = vsb.extract_files_from_spec(
        _spec(tmp_path, "# T\n**Status:** Ready for implementation\n\n## Files\n- `a.py`\n")
    )
    assert closed is False


# ── plan ────────────────────────────────────────────────────────────────


def _plan(paths):
    r = subprocess.run(
        [sys.executable, str(SCRIPTS / "validate_spec_boundaries.py"), "plan", *paths], capture_output=True, text=True
    )
    return r.returncode, r.stdout.strip().splitlines(), r.stderr


def test_plan_dispatches_a_ready_spec(tmp_path):
    a = _spec(tmp_path, "**Status:** Ready for implementation\n\n## Files\n- `a.py`\n", "GEMINI_A.md")
    code, out, _ = _plan([a])
    assert code == 0 and out == [a]


def test_plan_skips_queued_and_closed_specs(tmp_path):
    a = _spec(tmp_path, "**Status:** QUEUED\n\n## Files\n- `a.py`\n", "GEMINI_A.md")
    b = _spec(tmp_path, "**Status:** DONE\n\n## Files\n- `b.py`\n", "GEMINI_B.md")
    c = _spec(tmp_path, "**Status:** Ready for implementation\n\n## Files\n- `c.py`\n", "GEMINI_C.md")
    code, out, err = _plan([a, b, c])
    assert code == 0 and out == [c]
    assert "SKIP (QUEUED)" in err and "SKIP (CLOSED)" in err


def test_plan_defers_a_same_file_contender_without_dropping_the_batch(tmp_path):
    """One conflict must not poison the cycle.

    Each spec gets its own branch, so a deferred spec simply waits. Failing the
    whole batch would let a single contender stall all progress.
    """
    a = _spec(tmp_path, "**Status:** Ready for implementation\n\n## Files\n- `shared.py`\n", "GEMINI_A.md")
    b = _spec(tmp_path, "**Status:** Ready for implementation\n\n## Files\n- `shared.py`\n", "GEMINI_B.md")
    c = _spec(tmp_path, "**Status:** Ready for implementation\n\n## Files\n- `other.py`\n", "GEMINI_C.md")
    code, out, err = _plan([a, b, c])
    assert code == 0
    assert out == [a, c]
    assert "DEFER: GEMINI_B.md" in err


def test_plan_blocks_a_spec_with_no_files_section(tmp_path):
    """Changed 2026-07-29 (review): previously a warning that still dispatched.

    An undeclared boundary means an unbounded diff, which defeats the entire
    enforcement layer — the executor could touch anything and validate-diff
    would have nothing to compare against.
    """
    a = _spec(tmp_path, "**Status:** Ready for implementation\n\nNo files section.\n", "GEMINI_A.md")
    code, out, err = _plan([a])
    assert code == 0 and out == []
    assert "SKIP (NO ## Files)" in err


def test_plan_blocks_a_spec_declaring_a_protected_path(tmp_path):
    """The architect writes the specs, so without this check a spec declaring
    AI_CONTEXT.md or the pipeline scripts would pass its own boundary
    validation, auto-merge, and push. Self-modification of the guardrails is
    human-only."""
    a = _spec(
        tmp_path,
        "**Status:** Ready for implementation\n\n## Files\n- `AI_CONTEXT.md`\n",
        "GEMINI_A.md",
    )
    b = _spec(
        tmp_path,
        "**Status:** Ready for implementation\n\n## Files\n- `scripts/implement.sh`\n",
        "GEMINI_B.md",
    )
    c = _spec(
        tmp_path,
        "**Status:** Ready for implementation\n\n## Files\n- `collect_sjc.py`\n",
        "GEMINI_C.md",
    )
    code, out, err = _plan([a, b, c])
    assert code == 0 and out == [c]
    assert err.count("SKIP (PROTECTED)") == 2


@pytest.mark.parametrize(
    "path,expected",
    [
        ("AI_CONTEXT.md", True),
        ("scripts/validate_spec_boundaries.py", True),
        (".claude/agents/software-architect.md", True),
        ("ROADMAP.md", True),
        ("Makefile", True),
        ("./scripts/implement.sh", True),
        ("collect_sjc.py", False),
        ("tests/test_sweep.py", False),
        ("reviews/some-file.md", False),
    ],
)
def test_protected_predicate(path, expected):
    assert vsb.protected(path) is expected


def test_plan_fails_hard_on_a_missing_spec(tmp_path):
    code, _, err = _plan([str(tmp_path / "GEMINI_MISSING.md")])
    assert code == 1
    assert "not found" in err


# ── spec_attempts ledger ────────────────────────────────────────────────


def _attempts(tmp_path, *args):
    """Run spec_attempts.py against a ledger redirected into tmp_path."""
    ledger = str(tmp_path / "spec-attempts.json")
    code = (
        f"import sys; sys.path.insert(0, {str(SCRIPTS)!r}); import spec_attempts as sa; "
        "from pathlib import Path; "
        f"sa.FILE = Path({ledger!r}); sa.LOCK = sa.FILE.with_suffix('.json.lock'); "
        f"sys.argv = ['spec_attempts'] + {list(args)!r}; sa.main()"
    )
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    return r.returncode, r.stdout.strip(), r.stderr


def _ledger(tmp_path):
    p = tmp_path / "spec-attempts.json"
    return json.loads(p.read_text()) if p.exists() else {}


def test_bump_increments_attempts(tmp_path):
    _attempts(tmp_path, "bump", "GEMINI_A.md", "2026-07-29")
    assert _ledger(tmp_path)["GEMINI_A.md"]["attempts"] == 1


def test_merge_resets_the_attempt_count(tmp_path):
    for _ in range(2):
        _attempts(tmp_path, "bump", "GEMINI_A.md", "2026-07-29")
    _attempts(tmp_path, "record", "GEMINI_A.md", "MERGE", "2026-07-29")
    assert _ledger(tmp_path)["GEMINI_A.md"]["attempts"] == 0


def test_three_rejects_block_a_spec(tmp_path):
    for _ in range(3):
        _attempts(tmp_path, "bump", "GEMINI_A.md", "2026-07-29")
        _attempts(tmp_path, "record", "GEMINI_A.md", "REJECT", "2026-07-29")
    _, out, _ = _attempts(tmp_path, "blocked")
    assert out == "GEMINI_A.md"


def test_two_rejects_do_not_block(tmp_path):
    for _ in range(2):
        _attempts(tmp_path, "bump", "GEMINI_A.md", "2026-07-29")
        _attempts(tmp_path, "record", "GEMINI_A.md", "REJECT", "2026-07-29")
    _, out, _ = _attempts(tmp_path, "blocked")
    assert out == ""


def test_infra_outcomes_never_reach_the_reject_threshold(tmp_path):
    """The invariant that keeps an API outage from blocking real work.

    A retry after INFRA is not a fresh attempt, so ten consecutive outages leave
    the spec dispatchable.
    """
    for _ in range(10):
        _attempts(tmp_path, "bump", "GEMINI_A.md", "2026-07-29")
        _attempts(tmp_path, "record", "GEMINI_A.md", "INFRA", "2026-07-29")
    _, out, _ = _attempts(tmp_path, "blocked")
    assert out == ""
    assert _ledger(tmp_path)["GEMINI_A.md"]["attempts"] == 1


def test_infra_count_is_tracked_for_escalation(tmp_path):
    for _ in range(3):
        _attempts(tmp_path, "record", "GEMINI_A.md", "INFRA", "2026-07-29")
    _, out, _ = _attempts(tmp_path, "infra_count", "GEMINI_A.md")
    assert out == "3"


def test_a_reject_clears_the_infra_streak(tmp_path):
    _attempts(tmp_path, "record", "GEMINI_A.md", "INFRA", "2026-07-29")
    _attempts(tmp_path, "record", "GEMINI_A.md", "REJECT", "2026-07-29")
    _, out, _ = _attempts(tmp_path, "infra_count", "GEMINI_A.md")
    assert out == "0"


def test_no_output_refunds_the_attempt(tmp_path):
    """An executor that produced nothing says nothing about the spec."""
    _attempts(tmp_path, "bump", "GEMINI_A.md", "2026-07-29")
    _attempts(tmp_path, "no_output", "GEMINI_A.md")
    e = _ledger(tmp_path)["GEMINI_A.md"]
    assert e["attempts"] == 0
    assert e["no_output_count"] == 1


def test_attempts_never_go_negative(tmp_path):
    _attempts(tmp_path, "no_output", "GEMINI_A.md")
    assert _ledger(tmp_path)["GEMINI_A.md"]["attempts"] == 0
