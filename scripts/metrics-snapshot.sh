#!/usr/bin/env bash
# metrics-snapshot.sh — daily deterministic metrics snapshot.
# Appends one row to reviews/collection-metrics.jsonl. Independent of the phase
# chain; flock serializes it against the phases so it reads a consistent state.
set -uo pipefail

PROJECT_DIR="/home/san/Workspaces/county_crawler"
source "${PROJECT_DIR}/scripts/pipeline_id.sh" || exit 1
REVIEW_DIR="${PROJECT_DIR}/reviews"
LOG_FILE="${REVIEW_DIR}/${PIPELINE_ID}-metrics-snapshot.log"
PYTHON="${PYTHON_BIN:-python3}"

mkdir -p "${REVIEW_DIR}"
log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" >> "${LOG_FILE}"; }

PHASE="metrics-snapshot"
HEALTH_LOG="${REVIEW_DIR}/pipeline-health.log"
health_log() { echo "$(date '+%Y-%m-%d %H:%M:%S') ${PIPELINE_ID} ${PHASE} $*" >> "${HEALTH_LOG}"; }
trap 'rc=$?; if [ "${rc}" -eq 0 ]; then health_log PASS; else health_log "FAIL exit=${rc}"; fi' EXIT
health_log START

exec 9>"${PROJECT_DIR}/.pipeline.lock"
if ! flock -n 9; then
    log "Another pipeline phase is running — refusing to start"
    exit 0
fi

log "Appending metrics snapshot (${PIPELINE_ID})"
if ! "${PYTHON}" "${PROJECT_DIR}/scripts/collection_metrics.py" --append >> "${LOG_FILE}" 2>&1; then
    log "collection_metrics.py failed"
    exit 1
fi

# Commit the ledger so the trend survives, and so Phase 3's clean-tree check
# does not trip over it on the next cycle.
cd "${PROJECT_DIR}"
git add reviews/collection-metrics.jsonl reviews/pipeline-health.log 2>>"${LOG_FILE}" || true
if ! git diff --cached --quiet 2>/dev/null; then
    git commit -m "chore: metrics snapshot ${PIPELINE_ID}" >>"${LOG_FILE}" 2>&1 \
        || log "WARNING: snapshot commit failed"
fi

log "Metrics snapshot complete"
