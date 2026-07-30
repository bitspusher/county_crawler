#!/usr/bin/env python3
"""Spec attempts counter for the agent pipeline's circuit breaker.

State lives in reviews/spec-attempts.json as:
    {"GEMINI_FOO.md": {"attempts": N, "last_status": "...", "last_date": "...",
                       "infra_count": M, "no_output_count": K}}

A spec that has been rejected THRESHOLD times consecutively is blocked: the
architect must not approve it again and the dispatcher refuses it even if the
architect slips. That stops the pipeline from burning slots relitigating the same
bad spec forever, and escalates it to a human instead.

INFRA outcomes (auth failure, quota exhaustion, an empty agent response) are
tracked separately and do NOT count toward the reject threshold — the spec was
never really tried.

Usage:
    spec_attempts.py bump <spec> <date>        # attempt starting (status=PENDING)
    spec_attempts.py record <spec> <v> <date>  # verdict: MERGE resets the count
    spec_attempts.py blocked                   # list specs at/over the threshold
    spec_attempts.py infra_count <spec>        # print infra_count for one spec
"""

import fcntl
import json
import sys
from contextlib import contextmanager
from pathlib import Path

FILE = Path(__file__).resolve().parent.parent / "reviews" / "spec-attempts.json"
LOCK = FILE.with_suffix(".json.lock")
THRESHOLD = 3


@contextmanager
def _locked():
    # Exclusive advisory lock via a sidecar .lock file, guarding the
    # read-modify-write against overlapping pipeline runs.
    FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(LOCK, "w") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def _load() -> dict:
    return json.loads(FILE.read_text()) if FILE.exists() else {}


def _save(data: dict) -> None:
    FILE.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")


def _entry(data: dict, spec: str) -> dict:
    return data.setdefault(
        spec,
        {
            "attempts": 0,
            "last_status": None,
            "last_date": None,
            "infra_count": 0,
            "no_output_count": 0,
        },
    )


def bump(spec: str, date: str) -> None:
    """Register an attempt starting. Called on main, before any branch exists."""
    with _locked():
        data = _load()
        e = _entry(data, spec)
        # A retry after an INFRA outcome is not a fresh attempt at the spec:
        # nothing about the spec was tested last time.
        if e.get("last_status") != "INFRA":
            e["attempts"] = int(e.get("attempts", 0)) + 1
        e["last_status"] = "PENDING"
        e["last_date"] = date
        _save(data)


def record(spec: str, verdict: str, date: str) -> None:
    with _locked():
        data = _load()
        e = _entry(data, spec)
        if verdict == "MERGE":
            e["attempts"] = 0
            e["infra_count"] = 0
        elif verdict == "REJECT":
            e["infra_count"] = 0
        elif verdict == "INFRA":
            e["infra_count"] = int(e.get("infra_count", 0)) + 1
        e["last_status"] = verdict
        e["last_date"] = date
        _save(data)


def record_no_output(spec: str) -> None:
    """The executor produced no diff at all — refund the attempt.

    A no-op run says nothing about whether the spec is achievable, so charging it
    against the reject threshold would blame the spec for an executor failure.
    """
    with _locked():
        data = _load()
        e = _entry(data, spec)
        e["attempts"] = max(0, int(e.get("attempts", 0)) - 1)
        e["no_output_count"] = int(e.get("no_output_count", 0)) + 1
        _save(data)


def blocked() -> None:
    with _locked():
        data = _load()
    for spec, e in sorted(data.items()):
        if int(e.get("attempts", 0)) >= THRESHOLD and e.get("last_status") == "REJECT":
            print(spec)


def query_infra_count(spec: str) -> None:
    with _locked():
        data = _load()
    print(data.get(spec, {}).get("infra_count", 0))


def main() -> None:
    args = sys.argv[1:]
    if args[:1] == ["bump"] and len(args) == 3:
        bump(args[1], args[2])
    elif args[:1] == ["record"] and len(args) == 4:
        record(args[1], args[2], args[3])
    elif args[:1] == ["no_output"] and len(args) == 2:
        record_no_output(args[1])
    elif args == ["blocked"]:
        blocked()
    elif args[:1] == ["infra_count"] and len(args) == 2:
        query_infra_count(args[1])
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
