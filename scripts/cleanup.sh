#!/usr/bin/env bash
# Deterministic janitor — dry-run by default; pass --apply to act.
#
# Scope is deliberately narrow: it touches only things whose disposability is
# UNAMBIGUOUS.
#   1. scratch files (operator / probe leftovers),
#   2. AGED per-cycle review artifacts (regenerated every cycle; the durable
#      signal lives in pipeline-health.log and collection-metrics.jsonl),
#   3. specs the ARCHITECT explicitly promoted to `Status: ARCHIVED` — MOVED
#      (git mv, reversible) into docs/archived/, never deleted, and never on a
#      bare `DONE`, which can mean partially done.
#
# Spec archival is a judgment call owned by the architect: a spec stays at repo
# root until the architect is confident it is FULLY complete and sets
# `**Status:** ARCHIVED`. This script only files away what carries that marker.
#
# Never touches sjc.db, .browser_profile, or tests/fixtures/ — the database is
# append-only and irreplaceable without re-requesting from a county server, and
# a captured fixture is expensive to reacquire (it needs a human at a CAPTCHA).
#
# Usage:  scripts/cleanup.sh              # dry-run
#         scripts/cleanup.sh --apply      # act
#         scripts/cleanup.sh --days=14    # prune artifacts older than 14 days
set -euo pipefail
cd "$(dirname "$0")/.."

APPLY=0
DAYS=30
for a in "$@"; do
  case "$a" in
    --apply) APPLY=1 ;;
    --days=*) DAYS="${a#*=}" ;;
    *) echo "unknown arg: $a" >&2; exit 1 ;;
  esac
done

n=0
act() {  # act "<shell command>" "<description>"
  n=$((n + 1))
  if [ "$APPLY" = 1 ]; then echo "  [done] $2"; eval "$1"; else echo "  [would] $2"; fi
}

echo "== scratch files =="
for f in _tmp_*.py reviews/_manual-*.out reviews/_tmp_* .pipeline.lock; do
  [ -e "$f" ] && act "rm -f -- '$f'" "rm $f"
done

echo "== probe/debug dumps =="
if [ -d debug ]; then
  act "rm -rf -- debug" "rm -rf debug/ (raw portal dumps — re-created by --debug)"
fi

echo "== aged per-cycle review artifacts (> ${DAYS} days) =="
while IFS= read -r f; do
  [ -n "$f" ] && act "rm -f -- '$f'" "rm $(basename "$f")"
done < <(find reviews -maxdepth 1 -type f -mtime +"$DAYS" \( \
  -name '*-nightly-review.md'       -o -name '*-nightly-review.log' \
  -o -name '*-architect-decisions.md' -o -name '*-architect-review.log' \
  -o -name '*-post-review.md'       -o -name '*-post-review.log' \
  -o -name '*-summary.md'           -o -name '*-implement.log' \
  -o -name '*-implement-*.md'       -o -name '*-metrics-snapshot.log' \
  -o -name '*-compliance.log'       -o -name '*-rejection-feedback.md' \
  \) 2>/dev/null | sort)

echo "== architect-ARCHIVED specs -> docs/archived/ (move, reversible) =="
[ "$APPLY" = 1 ] && mkdir -p docs/archived
for s in GEMINI_*.md; do
  [ -e "$s" ] || continue
  if grep -qiE '^\*\*Status:\*\*[[:space:]]*ARCHIVED|^Status:[[:space:]]*ARCHIVED' "$s"; then
    act "mkdir -p docs/archived && git mv -- '$s' 'docs/archived/$s'" "git mv $s -> docs/archived/"
  fi
done

echo "== stale pipeline branches with no commits vs main =="
for b in $(git branch --list 'auto/*' 2>/dev/null | sed 's/^[* ]*//'); do
  if [ -z "$(git log "main..$b" --oneline 2>/dev/null)" ]; then
    act "git branch -D '$b'" "git branch -D $b (empty)"
  fi
done

echo "---"
echo "$n item(s) $([ "$APPLY" = 1 ] && echo 'cleaned' || echo 'would be cleaned — re-run with --apply to act')."
