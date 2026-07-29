#!/usr/bin/env bash
# nightly-review.sh — Phase 1. Run the three triage agents in parallel.
#   test-quality-auditor   — coverage gaps, tautological tests, silent skips
#   product-manager        — priorities, scope, the untested hypotheses
#   data-integrity-auditor — ways the dataset can be silently wrong
# Output: reviews/<PIPELINE_ID>-nightly-review.md, then chains to Phase 2.
set -uo pipefail

PROJECT_DIR="/home/san/Workspaces/county_crawler"
source "${PROJECT_DIR}/scripts/pipeline_id.sh" || exit 1
REVIEW_DIR="${PROJECT_DIR}/reviews"
source "${PROJECT_DIR}/scripts/agent_cost.sh"
DATE="${PIPELINE_DATE}"
REVIEW_FILE="${REVIEW_DIR}/${PIPELINE_ID}-nightly-review.md"
CLAUDE="${CLAUDE_BIN:-claude}"
LOG_FILE="${REVIEW_DIR}/${PIPELINE_ID}-nightly-review.log"

mkdir -p "${REVIEW_DIR}"

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" >> "${LOG_FILE}"; }

PHASE="nightly-review"
HEALTH_LOG="${REVIEW_DIR}/pipeline-health.log"
health_log() { echo "$(date '+%Y-%m-%d %H:%M:%S') ${PIPELINE_ID} ${PHASE} $*" >> "${HEALTH_LOG}"; }

# A quota stub is a short refusal-shaped reply, not findings. Detecting it lets
# the whole cycle short-circuit (exit 75) instead of writing an empty review that
# the architect then dutifully triages.
is_quota_stub() {
    local content="$1" bytes
    bytes=$(echo -n "${content}" | wc -c)
    if [ "${bytes}" -lt 500 ] && echo "${content}" | grep -qiE 'session limit|hit your.*limit|quota exhausted|rate limit.*resets|resets [0-9]{1,2}(:[0-9]{2})?\s*(am|pm)'; then
        return 0
    fi
    return 1
}

trap 'rc=$?; if [ "${rc}" -eq 75 ]; then health_log "QUOTA-EXHAUSTED $(date '\''+%Y-%m-%d %H:%M:%S'\'')"; elif [ "${rc}" -eq 0 ]; then health_log PASS; else health_log "FAIL exit=${rc}"; fi' EXIT
health_log START

# ── Concurrency lock ──────────────────────────────────────────────────
exec 9>"${PROJECT_DIR}/.pipeline.lock"
if ! flock -n 9; then
    log "Another pipeline phase is running — refusing to start"
    exit 0
fi

# Idempotency: one review per slot.
if [ -f "${REVIEW_FILE}" ]; then
    log "Nightly review already exists for ${PIPELINE_ID} — skipping"
    exit 0
fi

log "Starting nightly review (${PIPELINE_ID})"
log "PATH: ${PATH}"
log "CLAUDE_BIN resolved to: $(which "${CLAUDE}" 2>&1 || echo 'NOT FOUND')"
log "claude version: $("${CLAUDE}" --version 2>&1 || echo 'FAILED')"

auth_failed() {
    local out="$1"
    [ -z "${out}" ] && return 0
    echo "${out}" | grep -qiE 'invalid api key|authentication_error|unauthorized|fix external api key|rate[_ ]limit|quota|exceeded.*budget|cost limit|connection ?refused|unable to connect|network error|econnrefused' && return 0
    return 1
}
AUTH_CHECK=$("${CLAUDE}" -p "say ok" --model haiku --max-budget-usd 0.05 --dangerously-skip-permissions 2>>"${LOG_FILE}") || true
# OAuth refresh race: the first call can surface the pre-refresh "Invalid API
# key" even after the CLI has refreshed. Retry once.
if echo "${AUTH_CHECK}" | grep -qiE 'invalid api key|authentication_error'; then
    log "Auth check returned transient error: '${AUTH_CHECK}' — retrying after 3s"
    sleep 3
    AUTH_CHECK=$("${CLAUDE}" -p "say ok" --model haiku --max-budget-usd 0.05 --dangerously-skip-permissions 2>>"${LOG_FILE}") || true
fi
if auth_failed "${AUTH_CHECK}"; then
    log "ERROR: claude auth check failed — output: '${AUTH_CHECK}'"
    cat > "${REVIEW_FILE}" <<EOF
# Nightly Review — ${DATE}

**FAILED**: claude CLI auth check failed.
Output: \`${AUTH_CHECK}\`
Check credentials and network. See ${LOG_FILE}.
EOF
    exit 1
fi
log "Auth check passed: '${AUTH_CHECK}'"

# ── Deterministic metrics, computed before any agent runs ──────────────
# The agents get real numbers rather than estimating from the code. This is the
# whole reason collection_metrics.py exists.
METRICS=$(cd "${PROJECT_DIR}" && python3 scripts/collection_metrics.py 2>>"${LOG_FILE}" || echo "metrics failed")

# ── Helper: run one agent, capture output + errors ─────────────────────
run_agent() {
    local agent_name="$1" prompt="$2" model="${3:-sonnet}" budget="${4:-1.00}" effort="${5:-medium}"
    local stderr_file raw_json output
    stderr_file=$(mktemp)

    # Prompt on stdin, not argv: a single argv string past MAX_ARG_STRLEN
    # (131072 bytes) makes execve fail outright, and these prompts carry the
    # metrics block plus a dismissed-findings pointer.
    raw_json=$(cd "${PROJECT_DIR}" && printf '%s' "${prompt}" | "${CLAUDE}" -p \
        --agent "${agent_name}" \
        --dangerously-skip-permissions \
        --model "${model}" \
        --effort "${effort}" \
        --max-budget-usd "${budget}" \
        --output-format json \
        2>"${stderr_file}") || true
    output=$(agent_cost_capture_result "nightly-review" "${agent_name}" "${model}" "${raw_json}")

    if [ -s "${stderr_file}" ]; then
        log "${agent_name} stderr:"
        cat "${stderr_file}" >> "${LOG_FILE}"
    fi
    rm -f "${stderr_file}"

    local line_count
    line_count=$(echo "${output}" | wc -l)
    if [ -z "${output}" ]; then
        log "ERROR: ${agent_name} returned empty output"
        echo "[ERROR] ${agent_name} agent returned empty output — check ${LOG_FILE}"
    elif auth_failed "${output}"; then
        log "ERROR: ${agent_name} returned API error: '${output}'"
        echo "[ERROR] ${agent_name} agent got API error: \`${output}\` — check ${LOG_FILE}"
    elif [ "${line_count}" -lt 3 ] && [ "${#output}" -lt 120 ]; then
        log "ERROR: ${agent_name} output suspiciously short (${line_count} lines): '${output}'"
        echo "[ERROR] ${agent_name} output too short to be real findings: \`${output}\`"
    else
        log "${agent_name} succeeded (${line_count} lines)"
        echo "${output}"
    fi
}

# ── Dismissed-findings ledger ──────────────────────────────────────────
# Passed as a file REFERENCE, not inlined: once it grows past MAX_ARG_STRLEN,
# inlining makes exec fail with "Argument list too long".
DISMISSED_FILE="${REVIEW_DIR}/dismissed-findings.md"
DISMISSED_ROTATE_BYTES=51200
if [ -f "${DISMISSED_FILE}" ]; then
    size=$(stat -c%s "${DISMISSED_FILE}" 2>/dev/null || echo 0)
    if [ "${size}" -gt "${DISMISSED_ROTATE_BYTES}" ]; then
        archive="${REVIEW_DIR}/dismissed-findings-archive-${DATE}.md"
        [ -e "${archive}" ] && archive="${REVIEW_DIR}/dismissed-findings-archive-${PIPELINE_ID}.md"
        mv "${DISMISSED_FILE}" "${archive}"
        log "Rotated dismissed-findings.md (${size} bytes) → ${archive}"
        cat > "${DISMISSED_FILE}" <<EOF
# Dismissed Findings Ledger
# Auto-rotated on ${DATE} — prior entries archived to $(basename "${archive}").
# Consult the archive only if a finding looks previously dismissed.

EOF
    fi
fi

DISMISSED_CONTEXT=""
if [ -f "${DISMISSED_FILE}" ]; then
    DISMISSED_CONTEXT="

IMPORTANT: Before reporting, read reviews/dismissed-findings.md — those findings were already triaged and dismissed by the architect. Do NOT re-report any of them. Report only NEW findings."
fi

SHARED_CONTEXT="

--- DETERMINISTIC PROJECT METRICS (computed, not estimated) ---
${METRICS}

Ground yourself in these numbers rather than inferring them from the code. Note
especially: if the fixture count shows any SYNTHETIC fixtures, the parser tests
are a regression lock and not a correctness check; and if collection reports NO
DATABASE, then nothing has ever been collected and every claim about the live
data is a prediction.${DISMISSED_CONTEXT}"

# ── Prompts ────────────────────────────────────────────────────────────
QA_PROMPT="Audit the test suite for county_crawler. Identify coverage gaps, untested failure paths, tautological assertions, silent skips, and edge cases real county data will produce. Focus on what changed recently — check git log. Report your top findings (P0 and P1 only) as markdown. Do NOT rewrite any file; report here. Under 800 words.${SHARED_CONTEXT}"

PM_PROMPT="Review county_crawler from a product perspective. Assess whether ROADMAP.md's priorities are still right, whether recent work moved a load-bearing hypothesis, and whether anything in flight is adding complexity that is not earned. Name anything that should be CUT or DEFERRED — a review that only adds is not doing this job. Report as markdown. Do NOT rewrite MVP.md or ROADMAP.md; report here. Under 800 words.${SHARED_CONTEXT}"

DI_PROMPT="You are the data-integrity auditor for county_crawler. Your PRIMARY job is to find ways the dataset can be silently wrong — absence stored as data, incomplete windows that read as complete, conflated NULLs, unvalidated assumptions carrying weight they cannot bear, append-only invariants leaking. For EACH finding give the cheapest concrete check that would confirm or kill it: a command, a query, a test, or a spot-check against a named source. An audit that only lists worries has failed. The standing open questions: whether Portal.xhr returns real data or the app shell (§5.1, human-gated, do NOT speculate on the answer); whether the \$1.10/\$1,000 transfer-tax rate holds for Stockton (§6.4); and whether a \$0.00 tax really identifies a lender credit-bid under §11926 (§6.5). Rank by severity. Three sharp findings beat ten vague ones. Report as markdown, under 800 words.${SHARED_CONTEXT}"

# ── Run the three in parallel ──────────────────────────────────────────
QA_TMP=$(mktemp); PM_TMP=$(mktemp); DI_TMP=$(mktemp)
log "Running test-quality, product, and data-integrity agents in parallel..."

run_agent "test-quality-auditor"   "${QA_PROMPT}" "sonnet" "2.00" "medium" > "${QA_TMP}" &
QA_PID=$!
run_agent "product-manager"        "${PM_PROMPT}" "sonnet" "2.00" "medium" > "${PM_TMP}" &
PM_PID=$!
run_agent "data-integrity-auditor" "${DI_PROMPT}" "opus"   "4.00" "high"   > "${DI_TMP}" &
DI_PID=$!

# Wait on the first agent and check for a quota stub. If quota is gone it is gone
# for all three, so kill the others rather than burning the slot.
wait ${QA_PID} 2>/dev/null; log "test-quality agent complete (exit $?)"
QA_OUTPUT=$(cat "${QA_TMP}")
if is_quota_stub "${QA_OUTPUT}"; then
    log "First triage agent output is a quota stub — short-circuiting cycle"
    kill ${PM_PID} ${DI_PID} 2>/dev/null || true
    rm -f "${QA_TMP}" "${PM_TMP}" "${DI_TMP}"
    exit 75
fi

wait ${PM_PID} 2>/dev/null; log "product agent complete (exit $?)"
wait ${DI_PID} 2>/dev/null; log "data-integrity agent complete (exit $?)"
PM_OUTPUT=$(cat "${PM_TMP}")
DI_OUTPUT=$(cat "${DI_TMP}")
rm -f "${QA_TMP}" "${PM_TMP}" "${DI_TMP}"

# ── Assemble ───────────────────────────────────────────────────────────
cat > "${REVIEW_FILE}" <<EOF
# Nightly Review — ${DATE}

_Auto-generated by nightly-review.sh at $(date '+%Y-%m-%d %H:%M:%S')_
_Slot: ${PIPELINE_ID}. For the architect to triage._

---

## Deterministic Metrics

\`\`\`
${METRICS}
\`\`\`

---

## Test Quality Audit

${QA_OUTPUT}

---

## Product Review

${PM_OUTPUT}

---

## Data Integrity Audit

${DI_OUTPUT}

---

## Action Items for Architect

> Triage the findings above. Test-quality findings may warrant new specs;
> product findings may shift priorities or cut scope; data-integrity findings are
> the ones most likely to be P0, because this project's characteristic failure is
> data that looks fine and is wrong.

_End of nightly review._
EOF

log "Nightly review complete → ${REVIEW_FILE}"

# ── Chain: Phase 2 ─────────────────────────────────────────────────────
exec 9>&-
log "Chaining → architect-review.sh"
"${PROJECT_DIR}/scripts/architect-review.sh" || log "Chain: architect-review.sh exited $?"
