#!/usr/bin/env bash
# implement.sh — Phase 3. The executor implements approved specs, one branch each.
# Creates auto/<PIPELINE_ID>-<SPEC_STEM>, implements, validates boundaries, runs
# tests, writes a report. Does NOT merge — Phase 4 owns that decision.
set -uo pipefail

decisions_is_stub() {
    local dec_file="$1"
    [ -f "${dec_file}" ] || return 1
    head -n 10 "${dec_file}" | grep -qE '^\*\*(FAILED|SKIPPED)\*\*' 2>/dev/null
}

# A "strand" is work that exists but is not on the branch — left in a stash, or
# never committed. It looks like a result and is not one, so it gets its own
# health-log marker rather than being folded into a generic failure.
assert_branch_diverged() {
    local branch="$1" spec="$2" stash_before="${3:-0}"

    local commit_count
    commit_count=$(git -C "${PROJECT_DIR}" rev-list --count main..HEAD -- . ':(exclude)reviews/' 2>/dev/null || echo 0)

    local current_stash_count new_stashes=0 stash_entry=""
    current_stash_count=$(cd "${PROJECT_DIR}" && git stash list 2>/dev/null | wc -l)
    if [ "${current_stash_count}" -gt "${stash_before}" ]; then
        new_stashes=$((current_stash_count - stash_before))
    fi
    if [ "${new_stashes}" -gt 0 ]; then
        stash_entry=$(cd "${PROJECT_DIR}" && git stash list 2>/dev/null | head -n "${new_stashes}" | grep -F "${branch}" | head -n 1 || true)
    fi

    local has_failure=0
    if [ -n "${stash_entry}" ]; then
        echo "[implement] STRAND: work left in stash: ${stash_entry}" >&2
        has_failure=1
    fi
    if [ "${commit_count}" -lt 1 ]; then
        echo "[implement] STRAND: branch has no commits outside reviews/ vs main" >&2
        has_failure=1
    fi

    if [ "${has_failure}" -eq 1 ]; then
        health_log "STRAND ${spec} ${PIPELINE_ID}"
        return 1
    fi
    return 0
}

preserve_reviews_and_stash() {
    local proj_dir="$1" spec_name="$2" branch_name="${3:-branch}" pipe_id="${4:-pipe}" log_f="${5:-${LOG_FILE:-/dev/null}}"

    git -C "${proj_dir}" add reviews/ 2>>"${log_f}"
    if ! git -C "${proj_dir}" diff --cached --quiet -- reviews/; then
        local out exit_code
        out=$(git -C "${proj_dir}" commit -m "chore(pipeline): preserve reviews/ artifacts before stashing ${spec_name}" 2>&1)
        exit_code=$?
        echo "${out}" >> "${log_f}"
        if [ ${exit_code} -ne 0 ]; then
            echo "[$(date '+%Y-%m-%d %H:%M:%S')] Retrying reviews/ commit for ${spec_name} (exit ${exit_code})" >> "${log_f}"
            git -C "${proj_dir}" add -A reviews/ 2>>"${log_f}"
            out=$(git -C "${proj_dir}" commit -m "chore(pipeline): preserve reviews/ artifacts before stashing ${spec_name}" 2>&1)
            exit_code=$?
            echo "${out}" >> "${log_f}"
            if [ ${exit_code} -ne 0 ]; then
                echo "[$(date '+%Y-%m-%d %H:%M:%S')] ERROR: AUTOCOMMIT_FAILED for ${spec_name}: ${out}" >> "${log_f}"
                git -C "${proj_dir}" restore --staged reviews/ 2>/dev/null || git -C "${proj_dir}" reset -q -- reviews/ 2>/dev/null || true
            fi
        fi
    fi

    local remaining
    remaining=$(git -C "${proj_dir}" status --porcelain --untracked-files=no 2>/dev/null -- ':(exclude)reviews/' ':(exclude,glob)GEMINI_*.md')
    if [ -n "${remaining}" ]; then
        echo "[$(date '+%Y-%m-%d %H:%M:%S')] ERROR: uncommitted changes remain on ${branch_name} — stashing" >> "${log_f}"
        cd "${proj_dir}" && git stash push -m "implement ${pipe_id} ${spec_name}" 2>>"${log_f}" -- ':(exclude)reviews/' ':(exclude,glob)GEMINI_*.md' || true
        local post_dirty
        post_dirty=$(git -C "${proj_dir}" status --porcelain --untracked-files=no 2>/dev/null)
        if [ -n "${post_dirty}" ]; then
            echo "WARNING: tree still dirty after stash (preserved by design): $(echo "${post_dirty}" | cut -c 4- | tr '\n' ' ' | xargs)" >> "${log_f}"
        fi
    fi
}

# Allow sourcing for tests without executing the pipeline.
[ "${1:-}" = "--source-only" ] && return 0 2>/dev/null || true

PROJECT_DIR="/home/san/Workspaces/county_crawler"
source "${PROJECT_DIR}/scripts/pipeline_id.sh"
REVIEW_DIR="${PROJECT_DIR}/reviews"
source "${PROJECT_DIR}/scripts/agent_cost.sh"
DATE="${PIPELINE_DATE}"
DECISIONS_FILE="${REVIEW_DIR}/${PIPELINE_ID}-architect-decisions.md"
LOG_FILE="${REVIEW_DIR}/${PIPELINE_ID}-implement.log"
# The executor. Defaults to the antigravity CLI, matching the chess-annotator
# setup this pipeline is ported from. Override with EXECUTOR_BIN.
EXECUTOR="${EXECUTOR_BIN:-${GEMINI_BIN:-agy}}"
BRANCH_PREFIX="auto/${PIPELINE_ID}"

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" >> "${LOG_FILE}"; }

PHASE="implement"
HEALTH_LOG="${REVIEW_DIR}/pipeline-health.log"
health_log() { echo "$(date '+%Y-%m-%d %H:%M:%S') ${PIPELINE_ID} ${PHASE} $*" >> "${HEALTH_LOG}"; }

trap 'rc=$?; if [ "${rc}" -eq 0 ]; then health_log PASS; else health_log "FAIL exit=${rc}"; fi' EXIT
health_log START

exec 9>"${PROJECT_DIR}/.pipeline.lock"
if ! flock -n 9; then
    log "Another pipeline phase is running — refusing to start"
    exit 0
fi

log "Starting implementation run (${PIPELINE_ID})"

STALE_STASH_COUNT=$(cd "${PROJECT_DIR}" && git stash list 2>/dev/null | wc -l)
if [ "${STALE_STASH_COUNT}" -gt 0 ]; then
    log "WARNING: ${STALE_STASH_COUNT} stale stash entries — triage them"
fi

if [ ! -f "${DECISIONS_FILE}" ]; then
    log "No architect decisions for ${PIPELINE_ID} — skipping"
    exit 0
fi
if decisions_is_stub "${DECISIONS_FILE}"; then
    log "ERROR: decisions file is a failure stub — skipping"
    exit 0
fi
if ! command -v "${EXECUTOR}" &>/dev/null; then
    log "ERROR: executor '${EXECUTOR}' not found on PATH. Set EXECUTOR_BIN."
    exit 1
fi

# Never start dirty. reviews/agent-costs.jsonl is this pipeline's own exhaust and
# is tracked, so an unscoped check sees it and the executor self-blocks. Exclude
# exactly that file — never all of reviews/, which holds real work.
DIRTY=$(cd "${PROJECT_DIR}" && git diff --stat -- . ':(exclude)reviews/agent-costs.jsonl' 2>/dev/null)
if [ -n "${DIRTY}" ]; then
    log "ERROR: working tree has uncommitted changes — refusing to create a branch"
    log "Uncommitted: ${DIRTY}"
    exit 1
fi

SPECS_MENTIONED=$(grep -oE 'GEMINI_[A-Za-z0-9_]+\.md' "${DECISIONS_FILE}" 2>/dev/null | sort -u || echo "")
if [ -z "${SPECS_MENTIONED}" ]; then
    log "No specs referenced in architect decisions — skipping"
    exit 0
fi
log "Specs referenced: ${SPECS_MENTIONED}"

# Circuit-breaker backstop, in case the architect approved a blocked spec anyway.
BLOCKED_SPECS=$(python3 "${PROJECT_DIR}/scripts/spec_attempts.py" blocked 2>/dev/null || echo "")

SPECS_TO_IMPLEMENT=""
for spec in ${SPECS_MENTIONED}; do
    if [ ! -f "${PROJECT_DIR}/${spec}" ]; then
        log "WARNING: referenced spec ${spec} not found — skipping"
        continue
    fi
    if echo "${BLOCKED_SPECS}" | grep -qx "${spec}"; then
        log "Circuit breaker: skipping ${spec} (>=3 consecutive rejects)"
        continue
    fi
    SPECS_TO_IMPLEMENT="${SPECS_TO_IMPLEMENT} ${spec}"
    log "Found spec: ${spec}"
done
if [ -z "${SPECS_TO_IMPLEMENT}" ]; then
    log "No implementable specs — skipping"
    exit 0
fi

# ── Dispatch planning ──────────────────────────────────────────────────
# Drops QUEUED and closed specs, defers same-file contenders. A QUEUED spec must
# not block the others: each spec gets its own branch, so one being deferred
# cannot poison the batch.
log "Planning dispatch set..."
SPEC_PATHS=""
for spec in ${SPECS_TO_IMPLEMENT}; do
    SPEC_PATHS="${SPEC_PATHS} ${PROJECT_DIR}/${spec}"
done

PLAN_STDERR=$(mktemp)
DISPATCH_PATHS=$(python3 "${PROJECT_DIR}/scripts/validate_spec_boundaries.py" plan ${SPEC_PATHS} 2>"${PLAN_STDERR}")
PLAN_EXIT=$?
if [ -s "${PLAN_STDERR}" ]; then
    log "Dispatch planning notes:"
    cat "${PLAN_STDERR}" >> "${LOG_FILE}"
    while IFS= read -r plan_line; do
        [ -n "${plan_line}" ] && health_log "PLAN ${plan_line}"
    done < "${PLAN_STDERR}"
fi
rm -f "${PLAN_STDERR}"

if [ ${PLAN_EXIT} -ne 0 ]; then
    log "ERROR: dispatch planning failed (exit ${PLAN_EXIT}) — aborting"
    health_log "PLAN_FAIL ${SPECS_TO_IMPLEMENT}"
    exit 1
fi

SPECS_TO_IMPLEMENT=""
for path in ${DISPATCH_PATHS}; do
    SPECS_TO_IMPLEMENT="${SPECS_TO_IMPLEMENT} $(basename "${path}")"
done
if [ -z "${SPECS_TO_IMPLEMENT}" ]; then
    log "No dispatchable specs after planning (all QUEUED, closed, or deferred)"
    exit 0
fi
log "Dispatch set:${SPECS_TO_IMPLEMENT}"

EXECUTOR_MODEL="${EXECUTOR_MODEL:-auto}"
log "Executor: ${EXECUTOR}  model: ${EXECUTOR_MODEL}"

ARCHITECT_DECISIONS=$(cat "${DECISIONS_FILE}")
IMPLEMENTED_BRANCHES=""

# ── Per-spec loop. Each spec gets its own branch so one failure cannot
# ── poison the batch. ──────────────────────────────────────────────────
for spec in ${SPECS_TO_IMPLEMENT}; do
    SPEC_STEM="${spec%.md}"
    BRANCH="${BRANCH_PREFIX}-${SPEC_STEM}"

    if cd "${PROJECT_DIR}" && git rev-parse --verify "${BRANCH}" &>/dev/null; then
        log "Branch ${BRANCH} already exists — skipping ${spec}"
        IMPLEMENTED_BRANCHES="${IMPLEMENTED_BRANCHES} ${BRANCH}"
        continue
    fi

    STASH_BEFORE=$(cd "${PROJECT_DIR}" && git stash list 2>/dev/null | wc -l)
    log "Implementing ${spec} on ${BRANCH}..."

    # Read the allow-list from the spec via the same parser post-review uses, so
    # the prompt and the enforcement cannot disagree.
    ALLOWED_FILES=$(python3 -c "
import sys
sys.path.insert(0, '${PROJECT_DIR}/scripts')
from validate_spec_boundaries import extract_files_from_spec
files, _, _ = extract_files_from_spec('${PROJECT_DIR}/${spec}')
print('\n'.join(files or []))
" 2>>"${LOG_FILE}" || echo "")

    SPEC_CONTENTS=$(cat "${PROJECT_DIR}/${spec}")

    PROMPT="You are implementing an approved change to county_crawler, a San Joaquin
County foreclosure tracker. You are on branch '${BRANCH}' — NOT main.

================================================================================
FILE ACCESS RULES — STRICTLY ENFORCED (post-review rejects violations)
================================================================================

YOU MAY MODIFY ONLY THESE FILES:
$(echo "${ALLOWED_FILES}" | sed 's/^/  - /' || echo "  (none declared)")

You MAY READ any file for context. Modify nothing outside the list above.

VIOLATIONS THAT WILL BE REJECTED:
- Touching a file outside the list, including to 'fix' something noticed in passing
- Changing a test's expectation to match buggy behaviour instead of fixing the bug
- Deleting, weakening, or xfail-ing a test to get green
- Widening a threshold or loosening an assertion without justification
- If pre-commit reformats a file outside the allowed list, revert that file
  before committing

HARD PROJECT RULES (from AI_CONTEXT.md — read it; violations are rejected even
if every test passes):
- Never store a derived value. Price and sale_class live in VIEWS only.
- The observation tables are append-only. INSERT only; no UPDATE, no DELETE.
- Zero rows where rows were expected is a FAILURE, not a result.
- A result cap is an error, never an empty page. Never catch ResultCapExceeded
  and continue as though the window was empty.
- Deterministic parsers only. No model calls in the collection or parsing path.
- Stay polite: serial requests, keep the delay, never defeat the CAPTCHA, never
  fetch /Web/cart.
- Never claim an address or APN — the index contains neither.
- A test that reaches the live portal is marked 'live', always.

IMPLEMENTATION RULES:
- Implement ONLY what the spec describes. No extra features, no refactors.
- Run the verification commands in the spec. \`pytest -m \"not live\" -q\` must pass.
- If tests fail, fix within the allowed files. If you cannot, document it.
- Commit with a message referencing the spec name.

ITERATION LIMITS (strictly enforced):
- Maximum 5 attempts to fix any failing test. After that, STOP, commit what you
  have, and document the failures.
- Do NOT rewrite the same function or file more than once. If the first approach
  fails, document why and move on.
- If you find yourself reverting and retrying the same change, STOP.
- Commit incrementally after each task; do not accumulate a giant diff.
- If the spec is more complex than expected, implement the simplest correct
  subset and document what you deferred.
- When in doubt, do less. This repo has no ground truth to catch a mistake — a
  small correct change beats a large broken one.

COMMIT RULES:
- You MUST commit before finishing. Do NOT merge to main. Leave commits on this
  branch.
- Run pre-commit and fix lint errors before committing.

--- ARCHITECT DECISIONS (context) ---
${ARCHITECT_DECISIONS}

--- SPEC TO IMPLEMENT ---
${SPEC_CONTENTS}"

    cd "${PROJECT_DIR}" && git checkout -b "${BRANCH}" 2>>"${LOG_FILE}"

    STDERR_FILE=$(mktemp); PROMPT_FILE=$(mktemp); RAW_OUT=$(mktemp)
    # The prompt goes on the executor's stdin via a FILE: a single argv string
    # past MAX_ARG_STRLEN (131072) makes execve fail instantly. The executor also
    # DROPS stdout when it detects no controlling TTY (cron, command
    # substitution) — a silent "succeeded but did nothing" mode — so it runs
    # under a pseudo-TTY via `script -qec` (-e propagates the real exit code).
    # `script` commandeers stdin, so the prompt file is redirected onto the
    # executor's stdin INSIDE the command string.
    printf '%s' "${PROMPT}" > "${PROMPT_FILE}"
    MODEL_FLAG=""
    if [ -n "${EXECUTOR_MODEL}" ] && [ "${EXECUTOR_MODEL}" != "auto" ]; then
        MODEL_FLAG="--model '${EXECUTOR_MODEL}'"
    fi
    _g_start=${SECONDS}
    # -k 60: SIGTERM alone is not enough. Under memory pressure the agent can be
    # thrashing in swap and never scheduled long enough to handle it. SIGKILL
    # needs no cooperation, so the bleed past the deadline is capped at 60s.
    ( cd "${PROJECT_DIR}" && timeout -k 60 2700 script -qec \
        "${EXECUTOR} -p 'Implement exactly the spec and architect context provided on standard input. Follow every rule in it.' ${MODEL_FLAG} --dangerously-skip-permissions --print-timeout 44m < '${PROMPT_FILE}'" \
        /dev/null ) > "${RAW_OUT}" 2>"${STDERR_FILE}"
    EXEC_EXIT=$?
    _g_dur_ms=$(( (SECONDS - _g_start) * 1000 ))
    # Strip the ANSI escapes and CRs the pty injects, so the checks below see
    # clean text.
    EXEC_OUTPUT=$(sed -r 's/\x1B\[[0-9;]*[A-Za-z]//g' "${RAW_OUT}" | tr -d '\r')
    rm -f "${PROMPT_FILE}" "${RAW_OUT}"

    if [ -s "${STDERR_FILE}" ]; then
        log "executor stderr (${spec}):"
        cat "${STDERR_FILE}" >> "${LOG_FILE}"
    fi
    rm -f "${STDERR_FILE}"

    # Infra failures (auth, quota, network) hit every remaining spec identically,
    # so they stay fatal for the run. A TIMEOUT is spec-local and must NOT abort
    # the batch: the executor often commits working code and then hangs, and a
    # blanket abort would throw that work away unreviewed AND skip the remaining
    # specs. Let a timeout fall through to the normal
    # autocommit/boundary/test/report path and let the machine-captured results
    # decide whether anything landed.
    EXEC_FATAL=0
    if echo "${EXEC_OUTPUT}" | grep -qiE 'invalid api key|authentication_error|unauthorized|rate[_ ]limit|quota|exceeded.*budget|cost limit|connection ?refused|unable to connect|network error|econnrefused|ineligibletiererror'; then
        EXEC_FATAL=1
    elif [ ${EXEC_EXIT} -ne 0 ] && [ ${EXEC_EXIT} -ne 124 ]; then
        EXEC_FATAL=1
    elif [ ${EXEC_EXIT} -eq 0 ] && [ -z "${EXEC_OUTPUT}" ]; then
        EXEC_FATAL=1   # exit 0 with no output is the silent no-op mode
    fi

    if [ ${EXEC_FATAL} -eq 1 ]; then
        log "ERROR: executor failed (exit ${EXEC_EXIT}). Sample: ${EXEC_OUTPUT:0:200}"
        exit 1
    elif [ ${EXEC_EXIT} -eq 124 ]; then
        log "Executor timed out on ${spec} — verifying whatever landed on ${BRANCH}"
    else
        log "Executor completed ${spec} ($(echo "${EXEC_OUTPUT}" | wc -l) lines)"
    fi

    agent_cost_record_null "implement" "executor" "${EXECUTOR_MODEL}" "${_g_dur_ms}" "no_cost_meter" || true

    # Safety net: commit if the executor did not.
    UNCOMMITTED_TRACKED=$(cd "${PROJECT_DIR}" && git diff --name-only HEAD 2>/dev/null)
    UNCOMMITTED_UNTRACKED=""
    if [ -n "${ALLOWED_FILES}" ]; then
        while IFS= read -r path; do
            [ -z "${path}" ] && continue
            if [ -e "${PROJECT_DIR}/${path}" ] && ! (cd "${PROJECT_DIR}" && git ls-files --error-unmatch "${path}" &>/dev/null); then
                UNCOMMITTED_UNTRACKED="${UNCOMMITTED_UNTRACKED} ${path}"
                log "Staging spec-declared NEW file: ${path}"
            fi
        done <<< "${ALLOWED_FILES}"
    fi

    AUTOCOMMIT_STATUS="not needed"
    if [ -n "${UNCOMMITTED_TRACKED}" ] || [ -n "${UNCOMMITTED_UNTRACKED}" ]; then
        log "WARNING: executor left uncommitted changes for ${spec} — auto-committing"
        cd "${PROJECT_DIR}" && git add ${UNCOMMITTED_TRACKED} ${UNCOMMITTED_UNTRACKED} 2>>"${LOG_FILE}"
        COMMIT_OUTPUT=$(cd "${PROJECT_DIR}" && git commit -m "${BRANCH}: implementation (auto-committed)

Spec: ${spec}
Note: the executor did not commit. Auto-committed by implement.sh safety net." 2>&1)
        COMMIT_EXIT=$?
        echo "${COMMIT_OUTPUT}" >> "${LOG_FILE}"
        if [ ${COMMIT_EXIT} -ne 0 ]; then
            log "ERROR: AUTOCOMMIT_FAILED for ${spec} (exit ${COMMIT_EXIT}) — work is NOT on the branch: ${COMMIT_OUTPUT}"
            AUTOCOMMIT_STATUS="AUTOCOMMIT_FAILED (exit ${COMMIT_EXIT})"
        else
            AUTOCOMMIT_STATUS="ok"
        fi
    fi

    log "Validating diff boundaries for ${spec}..."
    cd "${PROJECT_DIR}"
    BOUNDARY_CHECK=$(python3 "${PROJECT_DIR}/scripts/validate_spec_boundaries.py" validate-diff "${PROJECT_DIR}/${spec}" "${BRANCH}" 2>&1)
    BOUNDARY_EXIT=$?
    log "Boundary validation (exit ${BOUNDARY_EXIT}): ${BOUNDARY_CHECK}"
    [ ${BOUNDARY_EXIT} -ne 0 ] && log "WARNING: boundary violation — will be rejected in post-review"

    log "Running tests for ${spec}..."
    TEST_OUTPUT=$(cd "${PROJECT_DIR}" && python3 -m pytest -m "not live" --tb=short -q 2>&1) || true
    TEST_EXIT=$?
    log "Test results (exit ${TEST_EXIT}): ${TEST_OUTPUT}"

    COMMITS_ON_BRANCH=$(cd "${PROJECT_DIR}" && git log main..HEAD --oneline 2>/dev/null || echo "none")
    DIFF_STAT=$(cd "${PROJECT_DIR}" && git diff --stat main 2>/dev/null || echo "none")
    CHANGED_ON_BRANCH=$(git -C "${PROJECT_DIR}" diff --name-only main..HEAD 2>/dev/null || echo "")

    NOOP_BANNER=""
    if [ -n "${ALLOWED_FILES}" ]; then
        ANY_ALLOWED_CHANGED=0
        for allowed in ${ALLOWED_FILES}; do
            if echo "${CHANGED_ON_BRANCH}" | grep -qx "${allowed}"; then
                ANY_ALLOWED_CHANGED=1
                break
            fi
        done
        if [ "${ANY_ALLOWED_CHANGED}" -eq 0 ]; then
            NOOP_BANNER="
**WARNING: ZERO spec-declared files modified — probable executor no-op (INFRA), not a result.**"
            health_log "NOOP ${spec} ${PIPELINE_ID}"
        fi
    fi

    IMPL_FILE="${REVIEW_DIR}/${PIPELINE_ID}-implement-${SPEC_STEM}.md"
    cat > "${IMPL_FILE}" <<EOF
# Implementation Report — ${spec}

_Auto-generated by implement.sh at $(date '+%Y-%m-%d %H:%M:%S')_
_Branch: \`${BRANCH}\`_
_Spec: ${spec}_

**Status: AWAITING ARCHITECT REVIEW** — not merged to main.${NOOP_BANNER}

---

## Branch Summary

### Boundary Validation
\`\`\`
${BOUNDARY_CHECK}
\`\`\`
Status: $([ ${BOUNDARY_EXIT} -eq 0 ] && echo "✓ In-bounds" || echo "✗ BOUNDARY VIOLATION")

### Commits
Auto-commit: ${AUTOCOMMIT_STATUS}
\`\`\`
${COMMITS_ON_BRANCH}
\`\`\`

### Changed Files
\`\`\`
${DIFF_STAT}
\`\`\`

### Test Results (non-live)
\`\`\`
${TEST_OUTPUT}
\`\`\`

---

## Executor Self-Report (UNVERIFIED — agent-authored; the machine-captured results above are authoritative)

${EXEC_OUTPUT}
EOF

    log "Implementation report → ${IMPL_FILE}"
    IMPLEMENTED_BRANCHES="${IMPLEMENTED_BRANCHES} ${BRANCH}"

    preserve_reviews_and_stash "${PROJECT_DIR}" "${spec}" "${BRANCH}" "${PIPELINE_ID}" "${LOG_FILE}"
    if ! assert_branch_diverged "${BRANCH}" "${spec}" "${STASH_BEFORE}"; then
        log "STRAND detected for ${spec} — continuing cycle"
    fi
    cd "${PROJECT_DIR}" && git checkout main 2>>"${LOG_FILE}"
done

log "Implementation run complete — branches:${IMPLEMENTED_BRANCHES}"

# ── Chain: Phase 4 ─────────────────────────────────────────────────────
exec 9>&-
log "Chaining → architect-post-review.sh"
"${PROJECT_DIR}/scripts/architect-post-review.sh" || log "Chain: architect-post-review.sh exited $?"
