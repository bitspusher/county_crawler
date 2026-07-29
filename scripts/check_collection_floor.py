#!/usr/bin/env python3
"""check_collection_floor.py — post-merge regression gate.

The chess-annotator pipeline this is modelled on gates merges on a recognition
accuracy floor. county_crawler has no accuracy metric, so this gates on the
things that CAN regress today and that an agent has both motive and opportunity
to break while making a diff pass:

  1. **Test count must not fall.** The single most valuable check in the file.
     Deleting or xfail-ing a test is the cheapest way to turn red green, and a
     summary line cannot distinguish "fixed" from "removed the test".
  2. **Fixture provenance must not regress.** A captured fixture reverting to
     synthetic silently downgrades the parser suite from correctness check back
     to regression lock.
  3. **The default suite must actually pass.**
  4. **Collected data must not shrink.** The observation tables are append-only
     (AI_CONTEXT.md rule 2), so a falling row count means something deleted or
     rebuilt history.
  5. **No new capped windows.** A capped window is an incomplete window
     (rule 4), and it must not accumulate unnoticed.

The floor lives in reviews/collection-floor.json and ratchets upward: a run that
improves a number writes the better value back, so the gate tightens as the
project grows. `--update` accepts the current state as the new floor, which is
what a human does after deliberately removing tests.

Exit 0 if every check passes, 1 on any regression, 2 if the gate could not run.
"""

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FLOOR = ROOT / "reviews" / "collection-floor.json"

sys.path.insert(0, str(ROOT / "scripts"))
from collection_metrics import collection_health, fixture_provenance, test_counts  # noqa: E402


def load_floor():
    if not FLOOR.exists():
        return {}
    try:
        return json.loads(FLOOR.read_text())
    except json.JSONDecodeError:
        print(f"WARNING: {FLOOR.name} is unparseable — treating as no floor", file=sys.stderr)
        return {}


def save_floor(data):
    FLOOR.parent.mkdir(parents=True, exist_ok=True)
    FLOOR.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")


def run_suite():
    """Run the default (non-live) suite. Returns (ok, tail_of_output)."""
    try:
        r = subprocess.run(
            [sys.executable, "-m", "pytest", "-m", "not live", "-q", "--tb=short"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=900,
        )
    except subprocess.TimeoutExpired:
        return False, "pytest timed out after 900s"
    except Exception as e:
        return False, f"could not run pytest: {e}"
    return r.returncode == 0, "\n".join(r.stdout.strip().splitlines()[-15:])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--update", action="store_true", help="accept the current state as the new floor (human use only)")
    ap.add_argument("--no-run", action="store_true", help="skip running the suite; check the counted metrics only")
    args = ap.parse_args()

    floor = load_floor()
    failures, notes, current = [], [], {}

    # ── 1. test count ───────────────────────────────────────────────────
    tests = test_counts()
    n = tests.get("total")
    if n is None:
        failures.append("could not count tests — pytest collection failed")
    else:
        current["test_count"] = n
        prev = floor.get("test_count")
        if prev is None:
            notes.append(f"test_count floor initialised at {n}")
        elif n < prev:
            failures.append(
                f"TEST COUNT FELL: {prev} -> {n} ({prev - n} tests gone). "
                "Deleting or xfail-ing tests is not a fix. If the removal was "
                "deliberate, re-run with --update."
            )
        elif n > prev:
            notes.append(f"test_count improved {prev} -> {n} (floor ratcheted up)")
        else:
            notes.append(f"test_count steady at {n}")

    # ── 2. fixture provenance ───────────────────────────────────────────
    fx = fixture_provenance()
    if fx:
        current["fixtures_captured"] = fx["captured"]
        prev = floor.get("fixtures_captured")
        if prev is None:
            notes.append(f"fixtures_captured floor initialised at {fx['captured']}")
        elif fx["captured"] < prev:
            failures.append(
                f"CAPTURED FIXTURES FELL: {prev} -> {fx['captured']}. A captured "
                "fixture reverting to synthetic silently downgrades the parser "
                "suite (MVP.md §6.2)."
            )
        elif fx["captured"] > prev:
            notes.append(f"fixtures_captured improved {prev} -> {fx['captured']}")
        if fx["synthetic"]:
            notes.append(
                f"{fx['synthetic']} fixture(s) still synthetic — "
                "parser tests are a regression lock, not a correctness check"
            )

    # ── 3. the suite passes ─────────────────────────────────────────────
    if args.no_run:
        notes.append("suite not run (--no-run)")
    else:
        ok, tail = run_suite()
        if ok:
            notes.append("default suite passes")
        else:
            failures.append(f"DEFAULT SUITE FAILED:\n{tail}")

    # ── 4 & 5. collected data ───────────────────────────────────────────
    health = collection_health()
    if health is None:
        notes.append("no sjc.db — collection checks skipped (nothing collected yet)")
    else:
        current["index_observations"] = health["index_observations"]
        prev = floor.get("index_observations")
        if prev is None:
            notes.append(f"index_observations floor initialised at {health['index_observations']}")
        elif health["index_observations"] < prev:
            failures.append(
                f"OBSERVATION COUNT FELL: {prev} -> {health['index_observations']}. "
                "index_obs is append-only (AI_CONTEXT.md rule 2); a falling count "
                "means history was deleted or rebuilt."
            )

        current["windows_capped"] = health["windows_capped"]
        prev_capped = floor.get("windows_capped", 0)
        if health["windows_capped"] > prev_capped:
            failures.append(
                f"NEW CAPPED WINDOWS: {prev_capped} -> {health['windows_capped']}. "
                "A capped window is incomplete and stored nothing; narrow the "
                "window and re-run it (AI_CONTEXT.md rule 4)."
            )

        if health["windows_unfinished"]:
            notes.append(f"{health['windows_unfinished']} run_log window(s) never finished — a run was interrupted")

    # ── report ──────────────────────────────────────────────────────────
    for line in notes:
        print(f"  ok   {line}")
    for line in failures:
        print(f"  FAIL {line}")

    if args.update:
        merged = dict(floor)
        merged.update(current)
        save_floor(merged)
        print(f"\nfloor updated -> {FLOOR.relative_to(ROOT)}: {json.dumps(current, sort_keys=True)}")
        return 0

    if failures:
        print(f"\nGATE FAILED ({len(failures)} regression(s)). Floor left unchanged.")
        return 1

    # Ratchet: only ever raise a floor, never lower it.
    ratcheted = dict(floor)
    for k, v in current.items():
        if k == "windows_capped":
            # Capped windows only ratchet DOWN — fixing one should tighten the
            # gate, and a new one should always trip it.
            ratcheted[k] = min(v, floor.get(k, v))
        elif isinstance(v, int):
            ratcheted[k] = max(v, floor.get(k, v))
    if ratcheted != floor:
        save_floor(ratcheted)
        print(f"\nfloor ratcheted -> {json.dumps(ratcheted, sort_keys=True)}")

    print("\nGATE PASSED.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
