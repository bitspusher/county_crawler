#!/usr/bin/env bash
# agent_cost.sh — shared, NON-BREAKING per-run cost/token capture for pipeline
# stages. Source AFTER PROJECT_DIR / REVIEW_DIR / PIPELINE_ID are set.
#
# Appends ONE JSON line per agent run to reviews/agent-costs.jsonl. Cost capture
# must NEVER break a stage: every function returns 0 even on error, and the
# markdown a stage writes is derived from the SAME `.result` text the CLI printed
# to stdout before --output-format json existed — so review files stay identical.
#
# Row schema (one object per line):
#   timestamp             ISO-8601 with tz offset (capture time)
#   pipeline_id           PIPELINE_ID of the slot (or "unknown")
#   phase                 stage script: nightly-review | architect-review |
#                         gemini-implement | architect-post-review |
#                         metrics-snapshot | compliance-review
#   stage                 the specific agent/role run
#   model                 model slug as invoked
#   cost_usd              .total_cost_usd, or null when no meter is available
#   input_tokens          .usage.input_tokens                 (null if absent)
#   output_tokens         .usage.output_tokens                (null if absent)
#   cache_read_tokens     .usage.cache_read_input_tokens      (null if absent)
#   cache_creation_tokens .usage.cache_creation_input_tokens  (null if absent)
#   num_turns             .num_turns                          (null if absent)
#   duration_ms           .duration_ms, or measured wall-ms
#   captured              true if the numbers came from a real CLI meter
#   note                  null on success; a reason string on fallback

: "${REVIEW_DIR:=${PROJECT_DIR:-.}/reviews}"
AGENT_COSTS_LOG="${AGENT_COSTS_LOG:-${REVIEW_DIR}/agent-costs.jsonl}"

_ac_now() { date '+%Y-%m-%dT%H:%M:%S%z'; }

_ac_append_line() {
    { mkdir -p "$(dirname "${AGENT_COSTS_LOG}")" 2>/dev/null
      printf '%s\n' "$1" >> "${AGENT_COSTS_LOG}"; } 2>/dev/null || true
}

# agent_cost_capture_result PHASE STAGE MODEL RAW_JSON
#   Echoes the assistant result text to stdout (byte-identical to the pre-json
#   stdout once wrapped in $(...)). Appends a full cost row when RAW_JSON is
#   well-formed CLI JSON; otherwise treats RAW_JSON as the text and records a
#   null-cost row noting the fallback. Always returns 0.
agent_cost_capture_result() {
    local phase="$1" stage="$2" model="$3" raw="$4"
    local ts pid; ts=$(_ac_now); pid="${PIPELINE_ID:-unknown}"
    if command -v jq >/dev/null 2>&1 \
       && printf '%s' "${raw}" | jq -e 'type=="object" and has("result")' >/dev/null 2>&1; then
        local row
        row=$(printf '%s' "${raw}" | jq -c \
            --arg ts "${ts}" --arg pid "${pid}" --arg phase "${phase}" \
            --arg stage "${stage}" --arg model "${model}" \
            '{timestamp:$ts, pipeline_id:$pid, phase:$phase, stage:$stage, model:$model,
              cost_usd:(.total_cost_usd // null),
              input_tokens:(.usage.input_tokens // null),
              output_tokens:(.usage.output_tokens // null),
              cache_read_tokens:(.usage.cache_read_input_tokens // null),
              cache_creation_tokens:(.usage.cache_creation_input_tokens // null),
              num_turns:(.num_turns // null),
              duration_ms:(.duration_ms // null),
              captured:true, note:null}' 2>/dev/null) || row=""
        [ -n "${row}" ] && _ac_append_line "${row}"
        printf '%s' "${raw}" | jq -r '.result // ""' 2>/dev/null
    elif command -v jq >/dev/null 2>&1 \
       && printf '%s' "${raw}" | jq -e 'type=="object" and (.is_error==true or has("errors"))' >/dev/null 2>&1; then
        # An error response (e.g. error_max_budget_usd) has no .result but DOES
        # carry real spend — capture it, and emit a clean note, never raw JSON.
        local row errmsg
        row=$(printf '%s' "${raw}" | jq -c \
            --arg ts "${ts}" --arg pid "${pid}" --arg phase "${phase}" \
            --arg stage "${stage}" --arg model "${model}" \
            '{timestamp:$ts, pipeline_id:$pid, phase:$phase, stage:$stage, model:$model,
              cost_usd:(.total_cost_usd // null),
              input_tokens:(.usage.input_tokens // null),
              output_tokens:(.usage.output_tokens // null),
              cache_read_tokens:(.usage.cache_read_input_tokens // null),
              cache_creation_tokens:(.usage.cache_creation_input_tokens // null),
              num_turns:(.num_turns // null),
              duration_ms:(.duration_ms // null),
              captured:true, note:("error:" + ((.subtype // "unknown")|tostring))}' 2>/dev/null) || row=""
        [ -n "${row}" ] && _ac_append_line "${row}"
        errmsg=$(printf '%s' "${raw}" | jq -r '(.errors // [])[0] // .subtype // "unknown error"' 2>/dev/null)
        printf '[agent errored: %s]\n' "${errmsg}"
    else
        # Not CLI JSON: unsupported flag, non-JSON output, or jq missing.
        agent_cost_record_null "${phase}" "${stage}" "${model}" null "unparseable_or_missing_json"
        printf '%s' "${raw}"
    fi
    return 0
}

# agent_cost_record_null PHASE STAGE MODEL DURATION_MS NOTE
#   Records a row with cost/tokens null — for executors that expose no cost
#   meter. DURATION_MS may be a number or the literal null. Always returns 0.
agent_cost_record_null() {
    local phase="$1" stage="$2" model="$3" dur="${4:-null}" note="${5:-null}"
    local ts pid row; ts=$(_ac_now); pid="${PIPELINE_ID:-unknown}"
    [ -z "${dur}" ] && dur="null"
    if command -v jq >/dev/null 2>&1; then
        row=$(jq -nc --arg ts "$ts" --arg pid "$pid" --arg phase "$phase" \
              --arg stage "$stage" --arg model "$model" --arg note "$note" \
              --argjson dur "${dur}" \
              '{timestamp:$ts,pipeline_id:$pid,phase:$phase,stage:$stage,model:$model,
                cost_usd:null,input_tokens:null,output_tokens:null,cache_read_tokens:null,
                cache_creation_tokens:null,num_turns:null,duration_ms:$dur,
                captured:false,note:$note}' 2>/dev/null) || row=""
        [ -n "${row}" ] && _ac_append_line "${row}"
    fi
    return 0
}
