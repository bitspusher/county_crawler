#!/usr/bin/env bash
# architect-review.sh — Phase 2. The architect triages the nightly findings,
# writes GEMINI_*.md specs, and commits so Phase 3 starts on a clean tree.
# Output: reviews/<PIPELINE_ID>-architect-decisions.md, then chains to Phase 3.
set -uo pipefail

PROJECT_DIR="/home/san/Workspaces/county_crawler"
source "${PROJECT_DIR}/scripts/pipeline_id.sh" || exit 1
REVIEW_DIR="${PROJECT_DIR}/reviews"
source "${PROJECT_DIR}/scripts/agent_cost.sh"
DATE="${PIPELINE_DATE}"
REVIEW_FILE="${REVIEW_DIR}/${PIPELINE_ID}-nightly-review.md"
OUTPUT_FILE="${REVIEW_DIR}/${PIPELINE_ID}-architect-decisions.md"
CLAUDE="${CLAUDE_BIN:-claude}"
LOG_FILE="${REVIEW_DIR}/${PIPELINE_ID}-architect-review.log"

mkdir -p "${REVIEW_DIR}"

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" >> "${LOG_FILE}"; }

PHASE="architect-review"
HEALTH_LOG="${REVIEW_DIR}/pipeline-health.log"
health_log() { echo "$(date '+%Y-%m-%d %H:%M:%S') ${PIPELINE_ID} ${PHASE} $*" >> "${HEALTH_LOG}"; }

BUDGET_DEATH_DETECTED=0

check_budget_death() {
    local exit_code="$1"
    [ "${exit_code}" -eq 0 ] && return 1
    [ "${BUDGET_DEATH_DETECTED:-0}" -eq 1 ] && return 0
    local pattern="maximum budget|budget.*exceeded|Reached maximum budget"
    if echo "${ARCHITECT_RAW:-}" | grep -qiE "${pattern}" 2>/dev/null \
       || echo "${ARCHITECT_OUTPUT:-}" | grep -qiE "${pattern}" 2>/dev/null \
       || { [ -n "${STDERR_FILE:-}" ] && [ -f "${STDERR_FILE}" ] && grep -qiE "${pattern}" "${STDERR_FILE}" >/dev/null 2>&1; } \
       || { [ -f "${OUTPUT_FILE:-}" ] && grep -qiE "${pattern}" "${OUTPUT_FILE}" >/dev/null 2>&1; }; then
        return 0
    fi
    return 1
}

is_quota_stub() {
    local content="$1" bytes
    bytes=$(echo -n "${content}" | wc -c)
    if [ "${bytes}" -lt 500 ] && echo "${content}" | grep -qiE 'session limit|hit your.*limit|quota exhausted|rate limit.*resets|resets [0-9]{1,2}(:[0-9]{2})?\s*(am|pm)'; then
        return 0
    fi
    return 1
}

trap 'rc=$?; if [ "${rc}" -eq 75 ]; then health_log "QUOTA-EXHAUSTED $(date '\''+%Y-%m-%d %H:%M:%S'\'')"; elif [ "${rc}" -eq 0 ]; then health_log PASS; elif check_budget_death "${rc}"; then health_log "ARCHITECT BUDGET_DEATH"; else health_log "FAIL exit=${rc}"; fi' EXIT
health_log START

exec 9>"${PROJECT_DIR}/.pipeline.lock"
if ! flock -n 9; then
    log "Another pipeline phase is running — refusing to start"
    exit 0
fi

# Idempotency, but only for a SUCCESSFUL decisions file. A failure stub must not
# be sticky: an "Error: Exceeded USD budget" file that counted as done would
# starve Phase 3 of specs forever.
if [ -f "${OUTPUT_FILE}" ]; then
    if grep -qE '^\*\*(FAILED|SKIPPED)\*\*' "${OUTPUT_FILE}" \
       || grep -qiE 'exceeded.*budget|error: exceeded|invalid api key|authentication_error|unauthorized|rate[_ ]limit|quota|cost limit|connection ?refused|unable to connect' "${OUTPUT_FILE}"; then
        log "Prior decisions file is a failure/error stub — retrying"
        rm -f "${OUTPUT_FILE}"
    else
        log "Architect decisions already exist for ${PIPELINE_ID} — skipping"
        exit 0
    fi
fi

log "Starting architect review (${PIPELINE_ID})"

if [ ! -f "${REVIEW_FILE}" ]; then
    log "ERROR: No nightly review at ${REVIEW_FILE}"
    cat > "${OUTPUT_FILE}" <<EOF
# Architect Review — ${DATE}

**SKIPPED**: No nightly review found for ${PIPELINE_ID}. Check nightly-review.sh ran.
EOF
    exit 1
fi

if grep -q "^\[ERROR\]" "${REVIEW_FILE}"; then
    log "WARNING: Nightly review contains agent errors — proceeding with partial data"
fi

auth_failed() {
    local out="$1"
    [ -z "${out}" ] && return 0
    echo "${out}" | grep -qiE 'invalid api key|authentication_error|unauthorized|fix external api key|rate[_ ]limit|quota|exceeded.*budget|cost limit|connection ?refused|unable to connect|network error|econnrefused' && return 0
    return 1
}
AUTH_CHECK=$("${CLAUDE}" -p "say ok" --model haiku --max-budget-usd 0.05 --dangerously-skip-permissions 2>>"${LOG_FILE}") || true
if echo "${AUTH_CHECK}" | grep -qiE 'invalid api key|authentication_error'; then
    log "Auth check transient error: '${AUTH_CHECK}' — retrying after 3s"
    sleep 3
    AUTH_CHECK=$("${CLAUDE}" -p "say ok" --model haiku --max-budget-usd 0.05 --dangerously-skip-permissions 2>>"${LOG_FILE}") || true
fi
if auth_failed "${AUTH_CHECK}"; then
    log "ERROR: claude auth check failed — output: '${AUTH_CHECK}'"
    cat > "${OUTPUT_FILE}" <<EOF
# Architect Review — ${DATE}

**FAILED**: claude CLI auth check failed.
Output: \`${AUTH_CHECK}\`
See ${LOG_FILE}.
EOF
    exit 1
fi
log "Auth check passed: '${AUTH_CHECK}'"

# ── Context ────────────────────────────────────────────────────────────
NIGHTLY_REVIEW=$(cat "${REVIEW_FILE}")

EXISTING_SPECS=""
for spec in "${PROJECT_DIR}"/GEMINI_*.md; do
    [ -f "${spec}" ] || continue
    name=$(basename "${spec}")
    status=$(grep -m1 '^\*\*Status:\*\*' "${spec}" 2>/dev/null || echo "  (no status line)")
    EXISTING_SPECS="${EXISTING_SPECS}
- ${name}: ${status}"
done
[ -z "${EXISTING_SPECS}" ] && EXISTING_SPECS="none"

ROADMAP=""
[ -f "${PROJECT_DIR}/ROADMAP.md" ] && ROADMAP=$(cat "${PROJECT_DIR}/ROADMAP.md")

RECENT_COMMITS=$(cd "${PROJECT_DIR}" && git log --oneline -10 2>/dev/null || echo "git log failed")
UNCOMMITTED=$(cd "${PROJECT_DIR}" && git diff --stat 2>/dev/null || echo "")
METRICS=$(cd "${PROJECT_DIR}" && python3 scripts/collection_metrics.py 2>>"${LOG_FILE}" || echo "metrics failed")

# Previous decisions, for continuity across slots.
PREV_DECISIONS=""
LATEST_DECISIONS=$(ls -t "${REVIEW_DIR}"/*-architect-decisions.md 2>/dev/null | head -1)
if [ -n "${LATEST_DECISIONS}" ] && [ "${LATEST_DECISIONS}" != "${OUTPUT_FILE}" ]; then
    PREV_DECISIONS=$(cat "${LATEST_DECISIONS}")
fi

DISMISSED_FILE="${REVIEW_DIR}/dismissed-findings.md"
DISMISSED=""
[ -f "${DISMISSED_FILE}" ] && DISMISSED=$(cat "${DISMISSED_FILE}")

# ── Open GitHub issues ─────────────────────────────────────────────────
# READ-ONLY, deliberately. The pipeline triages issues and specs the work; it
# never comments on, labels, or closes them. Those are outward-facing actions on
# a PUBLIC repo, and an unattended agent should not take them — a wrong close is
# visible to everyone and cannot be un-sent.
#
# Every failure path here degrades to "none" rather than aborting the slot. A
# triage without the issue list is worse than one with it, but far better than no
# triage at all — and the health log records which happened, so a silently
# issue-blind pipeline cannot masquerade as a healthy one. The likeliest cause is
# cron: `gh` authenticates against the system keyring, which may be locked in a
# session with no desktop.
GH_ISSUES="none"
if ! command -v gh >/dev/null 2>&1; then
    log "WARNING: gh not on PATH — triaging without GitHub issues"
    health_log "GH_ISSUES unavailable (gh not found)"
elif ! ISSUES_JSON=$(cd "${PROJECT_DIR}" && timeout 60 gh issue list --state open \
        --limit 20 --json number,title,body,labels,updatedAt 2>>"${LOG_FILE}"); then
    log "WARNING: gh issue list failed (auth, network, or keyring) — triaging without GitHub issues"
    health_log "GH_ISSUES unavailable (gh issue list failed)"
else
    GH_ISSUES=$(printf '%s' "${ISSUES_JSON}" \
        | python3 "${PROJECT_DIR}/scripts/format_issues.py" 2>>"${LOG_FILE}") || GH_ISSUES="none"
    if [ "${GH_ISSUES}" = "none" ]; then
        log "No open GitHub issues (or the payload did not parse — see the log)"
        health_log "GH_ISSUES none"
    else
        ISSUE_NUMS=$(printf '%s' "${ISSUES_JSON}" | grep -oE '"number":[0-9]+' | grep -oE '[0-9]+' | tr '\n' ' ')
        log "Open GitHub issues fed to triage: ${ISSUE_NUMS}"
        health_log "GH_ISSUES ok (${ISSUE_NUMS})"
    fi
fi

REJECTION_FEEDBACK=""
LATEST_FEEDBACK=$(ls -t "${REVIEW_DIR}"/*-rejection-feedback.md 2>/dev/null | head -1)
[ -n "${LATEST_FEEDBACK}" ] && REJECTION_FEEDBACK=$(cat "${LATEST_FEEDBACK}")

# Circuit breaker: specs at 3 consecutive rejects are off-limits until a human
# re-scopes them. Flag them and exclude them from the architect's option space.
BLOCKED_SPECS=$(python3 "${PROJECT_DIR}/scripts/spec_attempts.py" blocked 2>/dev/null || echo "")
if [ -n "${BLOCKED_SPECS}" ]; then
    log "Circuit breaker tripped for: ${BLOCKED_SPECS}"
    NEEDS_HUMAN_FILE="${REVIEW_DIR}/needs-human-review.md"
    [ -f "${NEEDS_HUMAN_FILE}" ] || echo "# Specs needing human review" > "${NEEDS_HUMAN_FILE}"
    for spec in ${BLOCKED_SPECS}; do
        SPEC_PATH="${PROJECT_DIR}/${spec}"
        if [ -f "${SPEC_PATH}" ] && grep -qE '^\*\*Status:\*\* (DONE|ARCHIVED)' "${SPEC_PATH}"; then
            log "[escalation] skip ${spec}: already closed"
            continue
        fi
        grep -q "^- ${DATE}: ${spec}$" "${NEEDS_HUMAN_FILE}" 2>/dev/null \
            || echo "- ${DATE}: ${spec}" >> "${NEEDS_HUMAN_FILE}"
    done
fi

# ── Prompt ─────────────────────────────────────────────────────────────
PROMPT=$(cat <<'PROMPT_EOF'
You have today's nightly review (test-quality, product, and data-integrity
findings), the open GitHub issues, plus project context and deterministic
metrics.

Your job:
1. TRIAGE each finding — act now, defer, or dismiss with a reason. Open GitHub
   issues are findings too, and rank alongside the agents'.
2. For items you approve, write or update GEMINI_*.md specs the implementer can
   execute. Follow SPEC_TEMPLATE.md exactly. Every spec MUST have a `## Files`
   section listing precisely which files may be modified — post-review enforces
   that boundary mechanically and an out-of-bounds diff is an automatic REJECT.
3. Produce a concise architect decisions document.

Rules:
- Write the decisions document FIRST, before authoring specs or verifying code,
  and update it incrementally. If your budget dies mid-run, that file is what
  survives.
- Do NOT implement code yourself. Write specs.
- Do NOT create a NEW spec for something an existing GEMINI_*.md already covers.
  Approve the existing one by referencing it in the decisions document.
- Do NOT approve anything that violates AI_CONTEXT.md. Check the relevant rules
  explicitly; a violation is grounds for rejection regardless of tests.
- Prefer small, safe changes. This repo is ~800 lines with NO ground truth to
  catch a mistake — a 20-line fix with a test beats a 200-line improvement.
- Do NOT approve work that presumes ROADMAP Phase 1's automation question is
  settled. Only a human running `scripts/probe_xhr.py --headed` can settle it.
- Do NOT spec Phase 0 items. They are a phone call to the Recorder, a CPRA
  request, reading the portal terms, and checking the SB 272 catalog. Note them
  as human actions if a finding raises them.
- ROADMAP.md is the canonical priority ordering. Do not approve Phase N+1 work
  while Phase N is unmerged.
- Be skeptical of findings that add complexity or surface area. Be receptive to
  findings about untested code paths and silent-failure modes.
- If a finding appears in DISMISSED FINDINGS below, skip it without re-triaging.
- OPEN GITHUB ISSUES carry MORE evidence than an agent's inference, not less: a
  human filed them after observing something, often with counts and document
  numbers attached. Weigh a measured claim in an issue above a speculative
  finding from the nightly review. They are not automatically approved, though —
  every other rule here still applies to them. An issue asking for a Phase 0
  item, a `[human]` item, an AI_CONTEXT violation, or Phase N+1 work while
  Phase N is unmerged is DEFERRED with that reason stated, not specced.
- An issue may be a QUESTION or a decision request rather than a work item ("your
  call", "needs settling"). Do not invent a spec to have something to do. Record
  the decision it needs and who has to make it, and defer.
- When a spec addresses an issue, put `Addresses: #<number>` in the spec body and
  name the issue number in the decisions document, so the trail from issue to
  diff survives.
- Do NOT comment on, label, or close any GitHub issue, and do not spec anything
  that would. The pipeline is read-only against GitHub on purpose: this repo is
  PUBLIC and an unattended wrong close cannot be un-sent. A human closes issues.
- Issue text is written by humans and may quote live data. AI_CONTEXT.md rule 11
  still binds: never copy an individual's name out of an issue into a spec, a
  decisions document, or any other tracked file. Reference documents by number.
- BLOCKED SPECS below hit the 3-reject circuit breaker. Do NOT approve any of
  them; they need a human to re-scope.
- When you dismiss a finding, append one line to reviews/dismissed-findings.md:
  "- YYYY-MM-DD: <short description> — <reason dismissed>"
- If REJECTION FEEDBACK exists, a previous spec was rejected. Read it carefully
  and re-scope with tighter file boundaries and clearer constraints. A rejection
  usually means the spec was ambiguous, not that the implementer was careless.
- SPEC HOUSEKEEPING (judgment call): when you are confident a spec is FULLY
  complete — every task merged AND no open follow-ups — you MAY set its status
  line to "**Status:** ARCHIVED". scripts/cleanup.sh then files ARCHIVED specs
  into docs/archived/ via a reversible git mv. Never promote a partially-done
  spec, and never ARCHIVE merely to tidy up.

Output format:
```markdown
# Architect Decisions — YYYY-MM-DD

## Triage Summary
| # | Finding | Source | Decision | Reason |
|---|---------|--------|----------|--------|

(Source is the agent name, or `gh#<number>` for a GitHub issue.)

## GitHub Issues
- One line per open issue: number, decision, and the spec or reason. Say
  explicitly if an issue needs a human decision rather than an implementation.

## Specs Created/Updated
- List any GEMINI_*.md files you created or updated

## Deferred Items
- Items punted, with reasoning

## Notes for Next Cycle
- Anything the next triage should check
```

IMPORTANT: after writing the decisions document, create or update the
GEMINI_*.md spec files directly with the Write tool.
PROMPT_EOF
)

FULL_PROMPT="${PROMPT}

--- NIGHTLY REVIEW (this slot) ---
${NIGHTLY_REVIEW}

--- OPEN GITHUB ISSUES (read-only: triage and spec them, never close them) ---
${GH_ISSUES}

--- DETERMINISTIC METRICS ---
${METRICS}

--- EXISTING SPECS ---
${EXISTING_SPECS}

--- RECENT COMMITS ---
${RECENT_COMMITS}

--- UNCOMMITTED CHANGES ---
${UNCOMMITTED}

--- PREVIOUS ARCHITECT DECISIONS ---
${PREV_DECISIONS:-none}

--- ROADMAP (canonical priority ordering — implement top-down) ---
${ROADMAP:-no roadmap found}

--- DISMISSED FINDINGS (skip these, already triaged) ---
${DISMISSED:-none}

--- REJECTION FEEDBACK (from a previous cycle's post-review) ---
${REJECTION_FEEDBACK:-none}

--- BLOCKED SPECS (3-reject circuit breaker — DO NOT approve) ---
${BLOCKED_SPECS:-none}"

log "Running architect (claude opus)..."
STDERR_FILE=$(mktemp)
AGENT_EXIT=0
ARCHITECT_RAW=$(cd "${PROJECT_DIR}" && printf '%s' "${FULL_PROMPT}" | "${CLAUDE}" -p \
    --agent software-architect \
    --dangerously-skip-permissions \
    --model opus \
    --effort high \
    --max-budget-usd 5.00 \
    --output-format json \
    2>"${STDERR_FILE}") || AGENT_EXIT=$?

if [ "${AGENT_EXIT}" -ne 0 ]; then
    if echo "${ARCHITECT_RAW:-}" | grep -qiE "maximum budget|budget.*exceeded|Reached maximum budget" \
       || { [ -f "${STDERR_FILE}" ] && grep -qiE "maximum budget|budget.*exceeded|Reached maximum budget" "${STDERR_FILE}" >/dev/null 2>&1; }; then
        BUDGET_DEATH_DETECTED=1
    fi
fi

if [ "${BUDGET_DEATH_DETECTED}" -eq 1 ]; then
    log "ERROR: budget cap exceeded during architect review"
    if [ ! -s "${OUTPUT_FILE}" ] || ! grep -qiE "maximum budget|budget.*exceeded" "${OUTPUT_FILE}"; then
        cat > "${OUTPUT_FILE}" <<EOF
# Architect Review — ${DATE}

**FAILED**: architect agent budget exceeded. See ${LOG_FILE}.
EOF
    fi
    exit 1
fi

ARCHITECT_OUTPUT=$(agent_cost_capture_result "architect-review" "software-architect" "opus" "${ARCHITECT_RAW}")

if [ -s "${STDERR_FILE}" ]; then
    log "claude stderr:"
    cat "${STDERR_FILE}" >> "${LOG_FILE}"
fi
rm -f "${STDERR_FILE}"

if is_quota_stub "${ARCHITECT_OUTPUT}"; then
    log "Architect output is a quota stub — short-circuiting cycle"
    exit 75
fi

if [ -z "${ARCHITECT_OUTPUT}" ]; then
    log "ERROR: architect returned empty output"
    cat > "${OUTPUT_FILE}" <<EOF
# Architect Review — ${DATE}

**FAILED**: architect agent returned empty output. See ${LOG_FILE}.
EOF
    exit 1
fi

log "Architect succeeded ($(echo "${ARCHITECT_OUTPUT}" | wc -l) lines)"

cat > "${OUTPUT_FILE}" <<EOF
# Architect Review — ${DATE}

_Auto-generated by architect-review.sh at $(date '+%Y-%m-%d %H:%M:%S')_
_Slot: ${PIPELINE_ID}_

---

${ARCHITECT_OUTPUT}
EOF

log "Architect review complete → ${OUTPUT_FILE}"

# ── Bump attempt counters ──────────────────────────────────────────────
# Must happen on main, not the feature branch: deleting a rejected branch would
# otherwise take the count with it and the circuit breaker would never trip.
APPROVED_SPECS=$(grep -oE 'GEMINI_[A-Za-z0-9_]+\.md' "${OUTPUT_FILE}" 2>/dev/null | sort -u || echo "")
for spec in ${APPROVED_SPECS}; do
    if [ -f "${PROJECT_DIR}/${spec}" ]; then
        python3 "${PROJECT_DIR}/scripts/spec_attempts.py" bump "${spec}" "${DATE}" 2>>"${LOG_FILE}" || true
        log "Bumped attempt counter for ${spec}"
    fi
done

# ── Commit Phase 1+2 output so Phase 3 starts clean ────────────────────
# The architect edits tracked specs and appends to dismissed-findings.md through
# its own tools, so the tree is always dirty here. Phase 3 refuses to run dirty,
# so without this commit the pipeline cannot progress past triage.
cd "${PROJECT_DIR}"

stage_if_exists() {
    for p in "$@"; do
        [ -e "${p}" ] && git add -- "${p}" 2>>"${LOG_FILE}" || true
    done
}

do_stage() {
    stage_if_exists \
        "reviews/${PIPELINE_ID}-nightly-review.md" \
        "reviews/${PIPELINE_ID}-architect-decisions.md" \
        "reviews/dismissed-findings.md" \
        "reviews/spec-attempts.json" \
        "reviews/needs-human-review.md" \
        "reviews/pipeline-health.log" \
        "reviews/collection-metrics.jsonl" \
        "reviews/collection-floor.json" \
        "ROADMAP.md"
    for spec in GEMINI_*.md; do
        [ -f "${spec}" ] && git add -- "${spec}" 2>>"${LOG_FILE}" || true
    done
}

do_stage

if ! git diff --cached --quiet 2>/dev/null; then
    STAGED_FILES=$(git diff --cached --name-only 2>/dev/null | tr '\n' ' ')
    log "Auto-committing Phase 1+2 triage outputs: ${STAGED_FILES}"
    COMMIT_MSG="chore: phase 1+2 triage outputs for ${PIPELINE_ID}

Auto-committed by architect-review.sh so Phase 3 starts on a clean tree."
    if ! git commit -m "${COMMIT_MSG}" >>"${LOG_FILE}" 2>&1; then
        # pre-commit may have auto-fixed whitespace/eof — re-stage and retry once.
        do_stage
        git commit -m "${COMMIT_MSG}" >>"${LOG_FILE}" 2>&1 || \
            log "WARNING: auto-commit failed — Phase 3 will likely block"
    fi
else
    log "No staged triage output changes to commit"
fi

# Runs unconditionally, to catch any dirty tracked file the staging list missed.
REMAINING=$(git status --porcelain --untracked-files=no 2>/dev/null)
if [ -n "${REMAINING}" ]; then
    log "WARNING: tree still dirty — staging and committing remainder"
    log "Remaining: ${REMAINING}"
    git add -u reviews/ 2>>"${LOG_FILE}" || true
    for spec in GEMINI_*.md; do
        [ -f "${spec}" ] && git add -- "${spec}" 2>>"${LOG_FILE}" || true
    done
    if ! git diff --cached --quiet 2>/dev/null; then
        git commit -m "chore: pipeline cleanup and triage outputs for ${PIPELINE_ID} (sweep)" \
            >>"${LOG_FILE}" 2>&1 || log "WARNING: sweep commit failed"
    fi
fi

# ── Chain: Phase 3 ─────────────────────────────────────────────────────
exec 9>&-
log "Chaining → implement.sh"
"${PROJECT_DIR}/scripts/implement.sh" || log "Chain: implement.sh exited $?"
