#!/usr/bin/env bash
# pipeline_id.sh — compute PIPELINE_ID for the current cadence slot.
# Source (don't execute) from each phase script immediately after PROJECT_DIR.
#
# Exports:
#   PIPELINE_ID   — "YYYY-MM-DD-HHMM", HHMM being the slot start
#                   (e.g. 2026-07-29-0600)
#   PIPELINE_DATE — "YYYY-MM-DD", for anything still scoped to a calendar day
#
# Slots: 00,06,12,18 :00 — a 6-hour stride, 4 runs/day.
#
# Why 6h and not the 2h the chess-annotator pipeline uses: this repo is ~800
# lines with 23 open roadmap items and a human-gated blocker at its centre
# (ROADMAP Phase 1's automation question, which no agent can resolve). At 12
# cycles/day the roadmap is exhausted inside a week and the architect starts
# inventing work to fill slots — which on a codebase with no ground truth is
# actively harmful. 4 cycles/day keeps the queue fed without manufacturing
# scope. Raise STRIDE_HOURS once collection is unblocked and there is real data
# to iterate against.
#
# Constraint: all four phases of a run must fire within the SAME slot. Sequential
# chaining completes a typical run in well under an hour, and the fallback cron
# offsets (+30/+60/+90 min) are all < 360 min, so they resolve to the same slot
# as the primary and the idempotency guards still correlate them.

STRIDE_HOURS="${STRIDE_HOURS:-6}"

# Honor a caller-provided PIPELINE_ID (hermetic tests pinning a fixed slot, or a
# downstream phase reusing the slot its parent chose). Compute from the wall
# clock ONLY when unset, so a pinned value stays deterministic instead of
# silently drifting with the date it happens to run on.
if [ -z "${PIPELINE_ID:-}" ]; then
    PIPELINE_DATE=$(date +%Y-%m-%d)
    _HOUR=$(date +%H)
    _HOUR=${_HOUR#0}            # strip the leading zero; bash reads 08 as octal
    _HOUR=${_HOUR:-0}           # "00" becomes "" after the strip
    _SLOT_HOUR=$(( (_HOUR / STRIDE_HOURS) * STRIDE_HOURS ))
    PIPELINE_ID="${PIPELINE_DATE}-$(printf '%02d00' ${_SLOT_HOUR})"
else
    if ! [[ "${PIPELINE_ID}" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}-[0-9]{4}$ ]]; then
        echo "Error: Invalid PIPELINE_ID format '${PIPELINE_ID}'. Expected YYYY-MM-DD-HHMM." >&2
        return 1 2>/dev/null || exit 1
    fi
    # Always derive PIPELINE_DATE from the caller's value (strip the -HHMM slot).
    PIPELINE_DATE="${PIPELINE_ID%-*}"
fi

export PIPELINE_ID
export PIPELINE_DATE
