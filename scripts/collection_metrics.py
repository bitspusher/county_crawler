#!/usr/bin/env python3
"""collection_metrics.py — deterministic project-health numbers.

Every number here is computed by this script from files on disk. None is
estimated, and none comes from an agent's judgment. That is the whole point: an
agent writing "the merge rate looks like about 60%" into a review is exactly the
failure this replaces.

The chess-annotator pipeline this is modelled on hill-climbs a recognition
accuracy metric. county_crawler has no accuracy metric yet, because it has no
collected data. What it has instead are **integrity and progress** metrics, which
are meaningful today:

  * roadmap progress   — unchecked boxes remaining, by phase
  * test surface       — collected test count, by marker
  * fixture provenance — how many fixtures are still synthetic (§6.2 wants zero)
  * spec throughput    — attempts, merges, rejects from the pipeline ledger
  * collection health  — only once sjc.db exists: windows covered, zero-row and
                         capped windows, and the §11926 tax distribution

Safe to run with no database and no pipeline history: absent inputs report as
null, never as zero, and never crash. "No data yet" and "zero" are different
claims and this script does not conflate them.

    python3 scripts/collection_metrics.py            # human-readable
    python3 scripts/collection_metrics.py --json      # one JSON object
    python3 scripts/collection_metrics.py --append    # + append to the ledger
"""

import argparse
import json
import re
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REVIEWS = ROOT / "reviews"
LEDGER = REVIEWS / "collection-metrics.jsonl"
DB = ROOT / "sjc.db"


# ── roadmap progress ─────────────────────────────────────────────────────


def roadmap_progress():
    """Count checkbox items in ROADMAP.md, per phase.

    A real progress signal: the roadmap is written as checkboxes and nothing else
    in the repo tracks how much of the plan is left.
    """
    path = ROOT / "ROADMAP.md"
    if not path.exists():
        return None
    phase, out = None, {}
    for line in path.read_text(encoding="utf-8").splitlines():
        m = re.match(r"^##\s+(Phase\s+\d+|Phase\s+0)\b", line)
        if m:
            phase = m.group(1)
            out.setdefault(phase, {"done": 0, "open": 0})
            continue
        if phase and re.match(r"^\s*-\s*\[[ xX]\]", line):
            key = "done" if re.match(r"^\s*-\s*\[[xX]\]", line) else "open"
            out[phase][key] += 1
    total_done = sum(p["done"] for p in out.values())
    total_open = sum(p["open"] for p in out.values())
    return {"by_phase": out, "done": total_done, "open": total_open}


# ── test surface ─────────────────────────────────────────────────────────


def test_counts():
    """Collected test counts by marker, via pytest --collect-only.

    Uses collection rather than a run so the metric is cheap and cannot be
    perturbed by a failure. The gate (check_collection_floor.py) is what actually
    runs them.
    """

    def collect(expr):
        try:
            r = subprocess.run(
                [sys.executable, "-m", "pytest", "--collect-only", "-q", "-m", expr],
                cwd=ROOT,
                capture_output=True,
                text=True,
                timeout=120,
            )
        except Exception:
            return None
        # Order matters. When a marker filter deselects some tests pytest prints
        # "N/M tests collected", and the bare `(\d+) tests collected` pattern
        # would match M out of that — reporting the total as the filtered count.
        # Check the slash form first.
        m = re.search(r"(\d+)/\d+\s+tests? collected", r.stdout)
        if m:
            return int(m.group(1))
        if re.search(r"no tests (collected|ran)", r.stdout):
            return 0
        m = re.search(r"(\d+)\s+tests? collected", r.stdout)
        return int(m.group(1)) if m else None

    return {
        "total": collect("not live"),
        "unit": collect("unit"),
        "integration": collect("integration"),
        "live": collect("live"),
    }


# ── fixture provenance ──────────────────────────────────────────────────


def fixture_provenance():
    """How many test fixtures are still synthetic.

    MVP.md §6.2 requires captured fixtures. Until this reads
    {"synthetic": 0}, a green parser suite does not mean the parsers work, and
    that distinction deserves a number rather than a footnote.
    """
    d = ROOT / "tests" / "fixtures"
    if not d.exists():
        return None
    synthetic = captured = 0
    for f in sorted(d.glob("*.html")):
        head = f.read_text(encoding="utf-8", errors="replace")[:400].upper()
        if "CAPTURED" in head:
            captured += 1
        else:
            synthetic += 1
    return {"synthetic": synthetic, "captured": captured}


# ── spec throughput ─────────────────────────────────────────────────────


def spec_stats():
    """Spec counts by status, plus ledger outcomes from the pipeline."""
    by_status = {}
    for spec in sorted(ROOT.glob("GEMINI_*.md")):
        m = re.search(r"^\*\*Status\*?\*?:?\*?\*?\s*(\S+)", spec.read_text(encoding="utf-8"), re.MULTILINE)
        key = m.group(1).strip().upper() if m else "NO_STATUS"
        by_status[key] = by_status.get(key, 0) + 1

    ledger_path = REVIEWS / "spec-attempts.json"
    ledger = None
    if ledger_path.exists():
        try:
            data = json.loads(ledger_path.read_text())
        except json.JSONDecodeError:
            data = {}
        outcomes = {}
        for e in data.values():
            s = e.get("last_status") or "UNKNOWN"
            outcomes[s] = outcomes.get(s, 0) + 1
        ledger = {
            "specs_tracked": len(data),
            "outcomes": outcomes,
            "blocked": sum(
                1 for e in data.values() if int(e.get("attempts", 0)) >= 3 and e.get("last_status") == "REJECT"
            ),
            "no_output_runs": sum(int(e.get("no_output_count", 0)) for e in data.values()),
        }
    return {"files_by_status": by_status, "total_files": sum(by_status.values()), "ledger": ledger}


# ── pipeline health ─────────────────────────────────────────────────────


def pipeline_health():
    path = REVIEWS / "pipeline-health.log"
    if not path.exists():
        return None
    counts = {}
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        for token in ("PASS", "FAIL", "START", "QUOTA-EXHAUSTED", "STRAND", "NOOP"):
            if f" {token}" in line:
                counts[token] = counts.get(token, 0) + 1
                break
    return counts or None


# ── collection health (needs sjc.db) ────────────────────────────────────


def collection_health():
    """Dataset integrity. Returns None when no database exists yet.

    None here means "never collected", which is the true state today and must not
    be reported as a set of zeros — that would read as "collected, found nothing".
    """
    if not DB.exists():
        return None
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    try:

        def one(sql, default=None):
            try:
                row = con.execute(sql).fetchone()
                return row[0] if row else default
            except sqlite3.Error:
                return default

        out = {
            "documents_indexed": one("SELECT COUNT(DISTINCT doc_number) FROM index_obs", 0),
            "details_fetched": one("SELECT COUNT(DISTINCT doc_number) FROM detail_obs WHERE fetch_ok=1", 0),
            "index_observations": one("SELECT COUNT(*) FROM index_obs", 0),
            "detail_fetch_failures": one("SELECT COUNT(*) FROM detail_obs WHERE fetch_ok=0", 0),
        }

        out["by_doc_type"] = {
            (t or "?"): c
            for t, c in con.execute("SELECT doc_type, COUNT(DISTINCT doc_number) FROM v_latest_index GROUP BY doc_type")
        }

        # run_log coverage. The zero-row and capped counts are the integrity
        # numbers that matter: they say how much of the dataset has a known hole.
        out["windows_attempted"] = one("SELECT COUNT(*) FROM run_log", 0)
        out["windows_zero_rows"] = one("SELECT COUNT(*) FROM run_log WHERE rows_indexed = 0", 0)
        out["windows_capped"] = one("SELECT COUNT(*) FROM run_log WHERE notes LIKE 'RESULT_CAP%'", 0)
        out["windows_unfinished"] = one("SELECT COUNT(*) FROM run_log WHERE finished_at IS NULL", 0)

        # The §11926 test (MVP.md §6.5). A bimodal distribution — a cluster at
        # exactly $0.00 and a spread of real values — supports the split. If
        # nothing is at zero, sale_class needs replacing.
        classes = {c: n for c, n in con.execute("SELECT sale_class, COUNT(*) FROM v_auction_sales GROUP BY sale_class")}
        priced = one("SELECT COUNT(*) FROM v_auction_sales WHERE derived_price IS NOT NULL", 0)
        out["sale_class"] = classes
        out["tdus_with_derived_price"] = priced
        total_tdus = sum(classes.values())
        out["zero_tax_fraction"] = round(classes.get("likely_reversion", 0) / total_tdus, 4) if total_tdus else None

        out["repeat_buyers"] = one("SELECT COUNT(*) FROM v_repeat_buyers", 0)
        return out
    finally:
        con.close()


# ── assembly ────────────────────────────────────────────────────────────


def snapshot():
    return {
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "git_commit": _git("rev-parse --short HEAD"),
        "git_branch": _git("rev-parse --abbrev-ref HEAD"),
        "roadmap": roadmap_progress(),
        "tests": test_counts(),
        "fixtures": fixture_provenance(),
        "specs": spec_stats(),
        "pipeline_health": pipeline_health(),
        "collection": collection_health(),
    }


def _git(args):
    try:
        r = subprocess.run(["git", *args.split()], cwd=ROOT, capture_output=True, text=True, timeout=10)
        return r.stdout.strip() or None
    except Exception:
        return None


def render(s):
    L = []
    L.append("=" * 68)
    L.append(f"county_crawler metrics — {s['timestamp']}  ({s.get('git_commit') or '?'})")
    L.append("=" * 68)

    r = s["roadmap"]
    if r:
        L.append(f"\nroadmap: {r['done']} done / {r['done'] + r['open']} items")
        for phase, c in r["by_phase"].items():
            L.append(f"   {phase:10s} {c['done']:2d} done, {c['open']:2d} open")

    t = s["tests"]
    L.append(f"\ntests collected: {t['total']}  (unit={t['unit']}, integration={t['integration']}, live={t['live']})")

    f = s["fixtures"]
    if f:
        L.append(f"fixtures: {f['captured']} captured, {f['synthetic']} SYNTHETIC")
        if f["synthetic"]:
            L.append("   ^ parser tests are a regression lock, not a correctness check")
            L.append("     (MVP.md §6.2 — run scripts/capture_fixtures.py --headed)")

    sp = s["specs"]
    L.append(f"\nspecs: {sp['total_files']} files {sp['files_by_status'] or ''}")
    if sp["ledger"]:
        lg = sp["ledger"]
        L.append(
            f"   ledger: {lg['specs_tracked']} tracked, outcomes={lg['outcomes']}, "
            f"blocked={lg['blocked']}, no-output runs={lg['no_output_runs']}"
        )

    if s["pipeline_health"]:
        L.append(f"pipeline health: {s['pipeline_health']}")

    c = s["collection"]
    L.append("")
    if c is None:
        L.append("collection: NO DATABASE YET — nothing has ever been collected.")
        L.append("   This is the true state, not a zero. ROADMAP Phase 1 is the blocker:")
        L.append("   run `python scripts/probe_xhr.py --headed` to settle it.")
    else:
        L.append(f"collection: {c['documents_indexed']} documents, {c['details_fetched']} details")
        L.append(f"   by type: {c['by_doc_type']}")
        L.append(
            f"   windows: {c['windows_attempted']} attempted, "
            f"{c['windows_zero_rows']} zero-row, {c['windows_capped']} CAPPED, "
            f"{c['windows_unfinished']} unfinished"
        )
        if c["windows_capped"]:
            L.append("   ^ capped windows are INCOMPLETE — narrow and re-run them")
        L.append(f"   sale_class: {c['sale_class']}   priced: {c['tdus_with_derived_price']}")
        zf = c["zero_tax_fraction"]
        if zf is not None:
            L.append(f"   zero-tax fraction: {zf:.1%}  (the §11926 test — expect bimodal)")
            if zf == 0.0:
                L.append("   ^ NOTHING at $0.00. The §11926 split may not exist in this")
                L.append("     county's data; sale_class needs replacing (§6.5).")
            elif zf == 1.0:
                L.append("   ^ EVERYTHING at $0.00. Tax is probably not recorded here at")
                L.append("     all, so derived_price is unavailable, not zero (§6.4).")
        L.append(f"   repeat buyers: {c['repeat_buyers']}")
    L.append("=" * 68)
    return "\n".join(L)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true", help="emit one JSON object")
    ap.add_argument("--append", action="store_true", help="append the snapshot to reviews/collection-metrics.jsonl")
    args = ap.parse_args()

    s = snapshot()
    if args.append:
        REVIEWS.mkdir(parents=True, exist_ok=True)
        with LEDGER.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(s, sort_keys=True) + "\n")
        print(f"appended snapshot -> {LEDGER.relative_to(ROOT)}")

    print(json.dumps(s, indent=2, sort_keys=True) if args.json else render(s))
    return 0


if __name__ == "__main__":
    sys.exit(main())
