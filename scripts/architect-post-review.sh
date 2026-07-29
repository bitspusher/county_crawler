#!/usr/bin/env bash
# architect-post-review.sh — Phase 4. The architect reviews each branch and
# decides MERGE or REJECT, then gates the merge on check_collection_floor.py.
set -uo pipefail

PROJECT_DIR="${PROJECT_DIR:-/home/san/Workspaces/county_crawler}"
source "${PROJECT_DIR}/scripts/pipeline_id.sh"
REVIEW_DIR="${PROJECT_DIR}/reviews"
source "${PROJECT_DIR}/scripts/agent_cost.sh"
DATE="${PIPELINE_DATE}"
OUTPUT_FILE="${REVIEW_DIR}/${PIPELINE_ID}-post-review.md"
CLAUDE="${CLAUDE_BIN:-claude}"
LOG_FILE="${REVIEW_DIR}/${PIPELINE_ID}-post-review.log"
BRANCH_PREFIX="auto/${PIPELINE_ID}"

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" >> "${LOG_FILE}"; }

PHASE="architect-post-review"
HEALTH_LOG="${REVIEW_DIR}/pipeline-health.log"
health_log() { echo "$(date '+%Y-%m-%d %H:%M:%S') ${PIPELINE_ID} ${PHASE} $*" >> "${HEALTH_LOG}"; }

# Runs on EVERY exit path from Phase 4, so early exits still produce the
# PIPELINE-FAILED marker when an upstream phase was missing or failed.
pipeline_health_check() {
    local expected="nightly-review architect-review implement architect-post-review"
    local run_entries="" failures=""
    [ -f "${HEALTH_LOG}" ] && run_entries=$(grep " ${PIPELINE_ID} " "${HEALTH_LOG}" || true)
    for phase in ${expected}; do
        # Our own PASS is appended by the trap after this runs.
        [ "${phase}" = "architect-post-review" ] && continue
        local has_start has_pass
        has_start=$(echo "${run_entries}" | grep -c " ${phase} START" || true)
        has_pass=$(echo "${run_entries}" | grep -c " ${phase} PASS" || true)
        if [ "${has_start}" -eq 0 ]; then
            failures="${failures} ${phase}(never-started)"
        elif [ "${has_pass}" -eq 0 ]; then
            failures="${failures} ${phase}(started-but-no-pass)"
        fi
    done
    local fail_file="${REVIEW_DIR}/${PIPELINE_ID}-PIPELINE-FAILED.md"
    if [ -n "${failures}" ]; then
        log "Pipeline health: failures →${failures}"
        cat > "${fail_file}" <<HEOF
# PIPELINE FAILED — ${DATE}

The pipeline did not complete cleanly for slot ${PIPELINE_ID}. Failing phases:
${failures}

See \`${HEALTH_LOG}\` for the raw phase log.
Per-phase logs: \`${REVIEW_DIR}/${PIPELINE_ID}-*.log\`.
HEOF
    else
        log "Pipeline health: all upstream phases passed"
        rm -f "${fail_file}"
    fi
}

MERGES_DONE=0
trap 'rc=$?; pipeline_health_check; if [ "${rc}" -eq 0 ]; then health_log "PASS merges=${MERGES_DONE}"; else health_log "FAIL exit=${rc}"; fi' EXIT
health_log START

exec 9>"${PROJECT_DIR}/.pipeline.lock"
if ! flock -n 9; then
    log "Another pipeline phase is running — refusing to start"
    exit 0
fi

if [ -f "${OUTPUT_FILE}" ]; then
    log "Post-review already exists for ${PIPELINE_ID} — skipping"
    exit 0
fi

log "Starting post-review (${PIPELINE_ID})"
DECISIONS_FILE="${REVIEW_DIR}/${PIPELINE_ID}-architect-decisions.md"

auth_failed() {
    local out="$1"
    [ -z "${out}" ] && return 0
    echo "${out}" | grep -qiE 'invalid api key|authentication_error|unauthorized|fix external api key|rate[_ ]limit|quota|exceeded.*budget|cost limit|connection ?refused|unable to connect|network error|econnrefused' && return 0
    return 1
}
AUTH_CHECK=$("${CLAUDE}" -p "say ok" --model haiku --max-budget-usd 0.05 --dangerously-skip-permissions 2>>"${LOG_FILE}") || true
if echo "${AUTH_CHECK}" | grep -qiE 'invalid api key|authentication_error'; then
    log "Auth check transient error — retrying after 3s"
    sleep 3
    AUTH_CHECK=$("${CLAUDE}" -p "say ok" --model haiku --max-budget-usd 0.05 --dangerously-skip-permissions 2>>"${LOG_FILE}") || true
fi
if auth_failed "${AUTH_CHECK}"; then
    log "ERROR: claude auth check failed — output: '${AUTH_CHECK}'"
    cat > "${OUTPUT_FILE}" <<EOF
# Post-Review — ${DATE}

**FAILED**: claude CLI auth check failed.
Output: \`${AUTH_CHECK}\`
Manual review required. Branches preserved.
EOF
    exit 1
fi
log "Auth check passed: '${AUTH_CHECK}'"

# ── Find branches ──────────────────────────────────────────────────────
BRANCHES_TO_REVIEW=""
cd "${PROJECT_DIR}"
for branch in $(git branch --list "${BRANCH_PREFIX}-*" 2>/dev/null | sed 's/^[* ]*//' || echo ""); do
    BRANCHES_TO_REVIEW="${BRANCHES_TO_REVIEW} ${branch}"
done

if [ -z "${BRANCHES_TO_REVIEW}" ]; then
    log "No branches for ${PIPELINE_ID} — nothing to review"
    exit 0
fi
log "Branches to review:${BRANCHES_TO_REVIEW}"

ARCHITECT_DECISIONS=""
[ -f "${DECISIONS_FILE}" ] && ARCHITECT_DECISIONS=$(cat "${DECISIONS_FILE}")

ALL_REVIEW_OUTPUT=""
OVERALL_STATUS=""
ANY_MERGED=false

for BRANCH in ${BRANCHES_TO_REVIEW}; do
    log "Reviewing ${BRANCH}..."
    SPEC_STEM=$(echo "${BRANCH}" | sed "s|${BRANCH_PREFIX}-||")
    SPEC_NAME="${SPEC_STEM}.md"

    COMMITS=$(git log main.."${BRANCH}" --oneline 2>/dev/null)
    # reviews/ is excluded: the pipeline's own artifacts are not the work.
    SUBSTANTIVE_COMMITS=$(git log main.."${BRANCH}" --oneline -- . ':(exclude)reviews/' 2>/dev/null)

    if [ -z "${SUBSTANTIVE_COMMITS}" ]; then
        VERDICT="REJECT"
        OUTCOME="REJECT"
        REVIEW_OUTPUT="Executor produced no diff outside reviews/ on ${BRANCH} during ${PIPELINE_ID}."
        log "Verdict for ${BRANCH}: REJECT (no substantive commits)"
    else
        IMPL_FILE="${REVIEW_DIR}/${PIPELINE_ID}-implement-${SPEC_STEM}.md"
        EXEC_REPORT=""
        [ -f "${IMPL_FILE}" ] && EXEC_REPORT=$(cat "${IMPL_FILE}")

        DIFF_STAT=$(git diff --stat main.."${BRANCH}" 2>/dev/null)
        FULL_DIFF=$(git diff main.."${BRANCH}" 2>/dev/null | head -500)
        DIFF_LINES=$(git diff main.."${BRANCH}" 2>/dev/null | wc -l)
        COMMIT_LOG=$(git log main.."${BRANCH}" --format="%h %s" 2>/dev/null)
        CHANGED_FILES=$(git diff --name-only main.."${BRANCH}" 2>/dev/null || echo "")

        # Machine boundary check, using the same parser the executor was given.
        BOUNDARY_RESULT=$(python3 "${PROJECT_DIR}/scripts/validate_spec_boundaries.py" \
            validate-diff "${PROJECT_DIR}/${SPEC_NAME}" "${BRANCH}" 2>&1)
        BOUNDARY_EXIT=$?
        ALLOWED_FILES=$(python3 -c "
import sys
sys.path.insert(0, '${PROJECT_DIR}/scripts')
from validate_spec_boundaries import extract_files_from_spec
files, _, _ = extract_files_from_spec('${PROJECT_DIR}/${SPEC_NAME}')
print('\n'.join(files or []))
" 2>/dev/null || echo "")

        if [ ${BOUNDARY_EXIT} -ne 0 ]; then
            FILE_BOUNDARY_NOTE="WARNING — FILE BOUNDARY VIOLATION (machine-checked):
${BOUNDARY_RESULT}"
        else
            FILE_BOUNDARY_NOTE="All modified files are within the spec's declared boundaries (machine-checked)."
        fi

        PROMPT="Review the implementation on branch '${BRANCH}' for spec '${SPEC_NAME}'
in county_crawler, and decide whether it merges.

Review criteria:
1. FILE BOUNDARIES — the spec's '## Files' section lists the allowed files.
   Anything outside it is a strong reason to REJECT. The machine check below is
   authoritative on this point.
2. SCOPE — does the diff do what the spec asked, and nothing more?
3. TESTS — do they pass? Were any deleted, xfail-ed, or weakened? Was a
   threshold widened or an assertion loosened to get green? Any of those is a
   REJECT.
4. AI_CONTEXT.md — check the relevant hard rules explicitly. A violation is a
   REJECT even if every test passes. Particularly: no stored derived values, no
   UPDATE/DELETE on observation tables, zero rows and result caps must stay
   loud failures, no model in the parsing path, no CAPTCHA circumvention, no
   claimed address or APN.
5. CORRECTNESS — obvious bugs, especially in parsing and in SQL.

${FILE_BOUNDARY_NOTE}

Treat the executor's self-report as UNVERIFIED. The machine-captured test output
and boundary result are authoritative; the agent's prose about what it did is not.

DECISION:
- Everything good: output 'VERDICT: MERGE' on its own line.
- Any issue: output 'VERDICT: REJECT' on its own line, with specific reasons.
- Output exactly one of them. Then write a brief review summary.

--- ARCHITECT DECISIONS (context) ---
${ARCHITECT_DECISIONS}

--- ALLOWED FILES (from spec) ---
${ALLOWED_FILES:-not specified}

--- MACHINE BOUNDARY CHECK (exit ${BOUNDARY_EXIT}) ---
${BOUNDARY_RESULT}

--- CHANGED FILES ---
${CHANGED_FILES}

--- COMMITS ---
${COMMIT_LOG}

--- DIFF STAT ---
${DIFF_STAT}

--- FULL DIFF (first 500 of ${DIFF_LINES} lines) ---
${FULL_DIFF}

--- EXECUTOR REPORT ---
${EXEC_REPORT}"

        STDERR_FILE=$(mktemp)
        REVIEW_RAW=$(cd "${PROJECT_DIR}" && printf '%s' "${PROMPT}" | "${CLAUDE}" -p \
            --agent software-architect \
            --dangerously-skip-permissions \
            --model opus \
            --effort high \
            --max-budget-usd 2.00 \
            --output-format json \
            2>"${STDERR_FILE}") || true
        REVIEW_OUTPUT=$(agent_cost_capture_result "architect-post-review" "software-architect" "opus" "${REVIEW_RAW}")

        if [ -s "${STDERR_FILE}" ]; then
            log "claude stderr (${BRANCH}):"
            cat "${STDERR_FILE}" >> "${LOG_FILE}"
        fi
        rm -f "${STDERR_FILE}"

        if [ -z "${REVIEW_OUTPUT}" ]; then
            log "ERROR: post-review returned empty for ${BRANCH}"
            REVIEW_OUTPUT="[ERROR] Architect agent returned empty output for ${BRANCH}."
        fi

        VERDICT=$(echo "${REVIEW_OUTPUT}" | grep -oE 'VERDICT:[[:space:]]*[A-Za-z]+' | head -1 | grep -oE '[A-Za-z]+$')
        log "Verdict for ${BRANCH}: ${VERDICT:-UNCLEAR}"
        OUTCOME=$(echo "${REVIEW_OUTPUT}" | python3 "${PROJECT_DIR}/scripts/review_outcome.py")

        # A machine-detected boundary violation overrides a MERGE verdict. The
        # boundary is mechanical, so an agent talking itself into merging past one
        # is exactly the failure the check exists to prevent.
        if [ "${OUTCOME}" = "MERGE" ] && [ ${BOUNDARY_EXIT} -ne 0 ]; then
            log "OVERRIDE: verdict was MERGE but the boundary check failed — forcing REJECT"
            OUTCOME="REJECT"
            REVIEW_OUTPUT="${REVIEW_OUTPUT}

---
**PIPELINE OVERRIDE:** the architect returned MERGE, but the machine boundary
check failed for this branch. Boundary enforcement is not a judgment call, so the
outcome was forced to REJECT.

\`\`\`
${BOUNDARY_RESULT}
\`\`\`"
        fi
    fi

    BRANCH_STATUS=""

    if [ "${OUTCOME}" = "MERGE" ]; then
        log "Merging ${BRANCH} into main..."
        if [ -n "$(git status --porcelain -- reviews/agent-costs.jsonl 2>/dev/null)" ]; then
            git add reviews/agent-costs.jsonl && git commit -m "chore: commit pending agent-costs telemetry before merge" >>"${LOG_FILE}" 2>&1
        fi

        git checkout main 2>>"${LOG_FILE}"
        git merge "${BRANCH}" --no-ff -m "Merge ${BRANCH}: agent pipeline

Reviewed by architect post-review ($(date '+%Y-%m-%d %H:%M'))
Spec: ${SPEC_NAME}" 2>>"${LOG_FILE}"
        MERGE_EXIT=$?

        if [ ${MERGE_EXIT} -eq 0 ]; then
            log "Merge successful for ${BRANCH}"
            BRANCH_STATUS="MERGED"
            ANY_MERGED=true
            MERGES_DONE=$((MERGES_DONE + 1))
            python3 "${PROJECT_DIR}/scripts/spec_attempts.py" record "${SPEC_NAME}" MERGE "${DATE}" 2>>"${LOG_FILE}" || true
        else
            DIRTY_PATHS=$(git status --porcelain 2>/dev/null | awk '{print $2}' | tr '\n' ' ')
            log "ERROR: merge failed for ${BRANCH} (exit ${MERGE_EXIT}) — dirty/conflicting: ${DIRTY_PATHS}"
            BRANCH_STATUS="MERGE FAILED"
            python3 "${PROJECT_DIR}/scripts/spec_attempts.py" record "${SPEC_NAME}" MERGE_FAILED "${DATE}" 2>>"${LOG_FILE}" || true
            NEEDS_HUMAN_FILE="${REVIEW_DIR}/needs-human-review.md"
            [ -f "${NEEDS_HUMAN_FILE}" ] || echo "# Specs needing human review" > "${NEEDS_HUMAN_FILE}"
            echo "- ${DATE} ${PIPELINE_ID}: MERGE FAILED for ${BRANCH} (VERDICT: MERGE) — conflicting paths: $(echo "${DIRTY_PATHS}" | xargs). Branch preserved; hand-merge or re-run." >> "${NEEDS_HUMAN_FILE}"
            git merge --abort 2>/dev/null
            git checkout main 2>>"${LOG_FILE}"
        fi

    elif [ "${OUTCOME}" = "INFRA" ]; then
        # The reviewer never formed a verdict. Preserve the branch and do NOT
        # count this against the reject threshold — the spec was never tried.
        python3 "${PROJECT_DIR}/scripts/spec_attempts.py" record "${SPEC_NAME}" INFRA "${DATE}" 2>>"${LOG_FILE}" || true
        INFRA_COUNT=$(python3 "${PROJECT_DIR}/scripts/spec_attempts.py" infra_count "${SPEC_NAME}" 2>/dev/null || echo "0")
        if [ "${INFRA_COUNT}" -ge 3 ]; then
            log "INFRA for ${BRANCH} escalated — 3 consecutive INFRA outcomes"
            NEEDS_HUMAN_FILE="${REVIEW_DIR}/needs-human-review.md"
            [ -f "${NEEDS_HUMAN_FILE}" ] || echo "# Specs needing human review" > "${NEEDS_HUMAN_FILE}"
            echo "- ${DATE}: ${SPEC_NAME} hit 3 consecutive INFRA outcomes on ${BRANCH} — a human must re-run the review or merge by hand; branch preserved" >> "${NEEDS_HUMAN_FILE}"
            BRANCH_STATUS="INFRA_ESCALATED"
        else
            log "INFRA for ${BRANCH} — verdict withheld, branch preserved for retry"
            BRANCH_STATUS="INFRA_RETRY"
        fi
        git checkout main 2>>"${LOG_FILE}" || true

    else
        log "Branch ${BRANCH} NOT merged — verdict: ${VERDICT:-UNCLEAR}"
        BRANCH_STATUS="REJECTED"
        python3 "${PROJECT_DIR}/scripts/spec_attempts.py" record "${SPEC_NAME}" REJECT "${DATE}" 2>>"${LOG_FILE}" || true
        # A no-output run says nothing about the spec's achievability, so refund
        # the attempt rather than charging the executor's failure to the spec.
        if [ -z "${COMMITS}" ]; then
            python3 "${PROJECT_DIR}/scripts/spec_attempts.py" no_output "${SPEC_NAME}" 2>>"${LOG_FILE}" || true
        fi
        git checkout main 2>>"${LOG_FILE}"
        git branch -D "${BRANCH}" 2>>"${LOG_FILE}" || true
        log "Deleted rejected branch ${BRANCH}"
    fi

    ALL_REVIEW_OUTPUT="${ALL_REVIEW_OUTPUT}

## ${BRANCH} — ${BRANCH_STATUS}

${REVIEW_OUTPUT}

---
"
    OVERALL_STATUS="${OVERALL_STATUS} ${SPEC_STEM}:${BRANCH_STATUS}"
done

# ── Post-merge regression gate ─────────────────────────────────────────
if ${ANY_MERGED}; then
    log "Running post-merge regression gate..."
    GATE_OUTPUT=$(cd "${PROJECT_DIR}" && python3 scripts/check_collection_floor.py 2>&1)
    GATE_EXIT=$?
    log "Gate (exit ${GATE_EXIT}):"
    echo "${GATE_OUTPUT}" >> "${LOG_FILE}"

    if [ ${GATE_EXIT} -ne 0 ]; then
        log "WARNING: regression gate FAILED after merge"
        OVERALL_STATUS="${OVERALL_STATUS} GATE:FAILED"
        cat > "${REVIEW_DIR}/${PIPELINE_ID}-REGRESSION.md" <<REOF
# REGRESSION GATE FAILED — ${PIPELINE_ID}

A merge landed and then failed \`scripts/check_collection_floor.py\`. The merge
is NOT reverted automatically — a human decides whether to revert or to accept
the new floor with \`--update\`.

\`\`\`
${GATE_OUTPUT}
\`\`\`
REOF
    else
        log "Gate passed. Appending a metrics snapshot..."
        (cd "${PROJECT_DIR}" && python3 scripts/collection_metrics.py --append >>"${LOG_FILE}" 2>&1) || \
            log "WARNING: metrics snapshot failed"

        # Push only when the gate is green, so a regression stays local.
        if git -C "${PROJECT_DIR}" remote get-url origin >/dev/null 2>&1; then
            log "Pushing main to origin..."
            git -C "${PROJECT_DIR}" push origin main >>"${LOG_FILE}" 2>&1 \
                && log "Push successful" \
                || log "WARNING: push failed — manual push needed"
        else
            log "No origin remote configured — skipping push"
        fi
    fi
fi

# ── Rejection feedback for the next cycle ──────────────────────────────
REJECTED_OUTCOMES=""; INFRA_RETRY_OUTCOMES=""
HAS_REJECTED=false; HAS_INFRA_RETRY=false
for item in ${OVERALL_STATUS}; do
    case "${item}" in
        *:REJECTED)    REJECTED_OUTCOMES="${REJECTED_OUTCOMES} ${item}"; HAS_REJECTED=true ;;
        *:INFRA_RETRY) INFRA_RETRY_OUTCOMES="${INFRA_RETRY_OUTCOMES} ${item}"; HAS_INFRA_RETRY=true ;;
    esac
done

if ${HAS_REJECTED} || ${HAS_INFRA_RETRY}; then
    FEEDBACK_FILE="${REVIEW_DIR}/${PIPELINE_ID}-rejection-feedback.md"
    cat > "${FEEDBACK_FILE}" <<FBEOF
# Rejection Feedback — ${PIPELINE_ID}

_For architect triage on the next cycle._

## Rejected:${REJECTED_OUTCOMES:- none}
## Infra retry:${INFRA_RETRY_OUTCOMES:- none}

## Architect review details

${ALL_REVIEW_OUTPUT}
FBEOF

    if ${HAS_REJECTED}; then
        cat >> "${FEEDBACK_FILE}" <<'FBEOF'

## Guidance for the next spec revision
- Tighten file boundaries. Every spec needs an explicit `## Files` section.
- If the executor exceeded scope, the spec was ambiguous — make it unambiguous
  rather than adding more prohibitions.
- If the executor produced no diff, the spec may be underspecified or the task
  may not be achievable within the declared files. Consider splitting it.
FBEOF
    fi
    log "Rejection feedback → ${FEEDBACK_FILE}"
fi

# ── Reports ────────────────────────────────────────────────────────────
cat > "${OUTPUT_FILE}" <<EOF
# Post-Review — ${PIPELINE_ID}

_Auto-generated by architect-post-review.sh at $(date '+%Y-%m-%d %H:%M:%S')_
_Branches reviewed:${BRANCHES_TO_REVIEW}_
_Results:${OVERALL_STATUS}_

---

${ALL_REVIEW_OUTPUT}
EOF

SUMMARY_FILE="${REVIEW_DIR}/${PIPELINE_ID}-summary.md"
cat > "${SUMMARY_FILE}" <<EOF
# Pipeline Summary — ${PIPELINE_ID}

| Phase | Status |
|-------|--------|
| 1. Triage (test-quality + product + data-integrity) | $([ -f "${REVIEW_DIR}/${PIPELINE_ID}-nightly-review.md" ] && echo "Done" || echo "Missing") |
| 2. Architect triage | $([ -f "${DECISIONS_FILE}" ] && echo "Done" || echo "Missing") |
| 3. Implementation | $(ls "${REVIEW_DIR}/${PIPELINE_ID}-implement-"*.md 2>/dev/null | wc -l) branch(es) |
| 4. Post-review |${OVERALL_STATUS} |

_Details in $(basename "${OUTPUT_FILE}")_
EOF

log "Post-review complete → ${OUTPUT_FILE}"
log "Summary → ${SUMMARY_FILE}"

# ── Prune per-run files, keeping cumulative ledgers ────────────────────
CLEANUP_AGE="${PIPELINE_CLEANUP_HOURS:-72}"
log "Cleaning per-run files older than ${CLEANUP_AGE}h..."
CLEANED=0
while IFS= read -r old_file; do
    case "$(basename "${old_file}")" in
        dismissed-findings.md|needs-human-review.md|spec-attempts.json|\
        pipeline-health.log|collection-metrics.jsonl|collection-floor.json|\
        agent-costs.jsonl) continue ;;
    esac
    rm -f "${old_file}"
    CLEANED=$((CLEANED + 1))
done < <(find "${REVIEW_DIR}" -maxdepth 1 \( -name "*.md" -o -name "*.log" \) \
    -mmin +$((CLEANUP_AGE * 60)) -type f 2>/dev/null)
log "Cleaned ${CLEANED} stale files"
