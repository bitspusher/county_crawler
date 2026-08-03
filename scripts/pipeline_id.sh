#!/usr/bin/env bash
# pipeline_id.sh — compute PIPELINE_ID for the current cadence slot.
# Source (don't execute) from each phase script immediately after PROJECT_DIR.
#
# Exports:
#   PIPELINE_ID   — "YYYY-MM-DD-HHMM", HHMM being the slot start
#                   (e.g. 2026-07-29-0600)
#   PIPELINE_DATE — "YYYY-MM-DD", for anything still scoped to a calendar day
#
# Slots: one per calendar day — a 24-hour stride, so PIPELINE_ID is always
# "YYYY-MM-DD-0000".
#
# Why daily and not the 2h the chess-annotator pipeline uses, or the 6h this
# pipeline was originally written for: this repo is ~1000 lines with a couple of
# dozen open roadmap items, several of them `[human]`-gated (a phone call to the
# Recorder, a headed CAPTCHA session, reading the portal terms) that no agent can
# resolve. Cycles do not create roadmap items; they consume them. At 4 cycles/day
# the queue empties inside a week and the architect starts inventing work to fill
# slots — actively harmful on a codebase with almost no ground truth to catch a
# bad change. One cycle/day matches the rate at which real work actually appears
# here, which is roughly "whatever a human noticed since yesterday" plus whatever
# is open on GitHub. Raise STRIDE_HOURS only if slots start going hungry with
# genuine work still queued.
#
# Constraint: all four phases of a run must fire within the SAME slot, so the
# idempotency guards correlate them. At a 24-hour stride that is automatic for
# anything firing on the same calendar day — the fallback offsets (+30/+60/+90
# min after a 03:10 primary) cannot cross midnight. The one thing that WOULD
# break it is scheduling the primary near 23:00, so do not.

STRIDE_HOURS="${STRIDE_HOURS:-24}"

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
