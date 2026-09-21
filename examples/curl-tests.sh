#!/usr/bin/env bash
#
# Reproduce the Azure GPT-OSS tool_choice failures and verify the shim fix.
#
# Requires:
#   UPSTREAM_URL          — Azure AI Foundry base URL
#   AZURE_FOUNDRY_API_KEY — API key
#   SHIM_URL              — (optional) shim URL, default http://127.0.0.1:9526
#   MODEL                 — (optional) deployment name, default gpt-oss-120b
#
# Exit status is non-zero if any check does not match the expected result.
#
set -euo pipefail

: "${UPSTREAM_URL:?UPSTREAM_URL is required}"
: "${AZURE_FOUNDRY_API_KEY:?AZURE_FOUNDRY_API_KEY is required}"
SHIM_URL="${SHIM_URL:-http://127.0.0.1:9526}"
MODEL="${MODEL:-gpt-oss-120b}"

FAILURES=0
pass() { echo "[PASS] $*"; }
fail() { echo "[FAIL] $*"; FAILURES=$((FAILURES + 1)); }

TOOLS='[{"type":"function","function":{"name":"get_files","description":"List files matching a glob.","parameters":{"type":"object","properties":{"pattern":{"type":"string"}},"required":["pattern"]}}}]'
MESSAGES='[{"role":"system","content":"You MUST call get_files."},{"role":"user","content":"List markdown files with pattern **/*.md."}]'
FORCED='{"type":"function","function":{"name":"get_files"}}'

# post URL TOOL_CHOICE [extra curl args...] -> sets STATUS and BODY
post() {
    local url="$1" tool_choice="$2"
    shift 2
    local out
    out=$(curl -sS --max-time 120 -w '\n%{http_code}' -X POST "$url" \
        -H "Content-Type: application/json" "$@" \
        -d "{\"model\":\"$MODEL\",\"messages\":$MESSAGES,\"tools\":$TOOLS,\"tool_choice\":$tool_choice,\"stream\":false,\"max_completion_tokens\":2048}")
    STATUS="${out##*$'\n'}"
    BODY="${out%$'\n'*}"
}

# Prints: empty | missing | tool_calls | no_tool_calls | not_json
classify() {
    printf '%s' "$BODY" | python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
except ValueError:
    print("not_json"); sys.exit()
if "choices" not in d:
    print("missing")
elif d["choices"] == []:
    print("empty")
elif d["choices"][0].get("message", {}).get("tool_calls"):
    print("tool_calls")
else:
    print("no_tool_calls")
'
}

show_body() { printf '       %s\n' "$(printf '%s' "$BODY" | head -c 300)"; }

AZURE_AUTH=(-H "api-key: $AZURE_FOUNDRY_API_KEY")

echo
echo "=== 1. Direct Azure — forced function tool_choice (expected: HTTP 200, choices=[]) ==="
post "$UPSTREAM_URL/v1/chat/completions" "$FORCED" "${AZURE_AUTH[@]}"
if [ "$STATUS" = "200" ] && [ "$(classify)" = "empty" ]; then
    pass "reproduced: HTTP 200 with choices=[]"
else
    fail "expected HTTP 200 with choices=[], got HTTP $STATUS ($(classify))"
    show_body
fi

echo
echo "=== 2. Direct Azure — tool_choice=\"required\" (expected: HTTP 400 UnsupportedToolUse) ==="
post "$UPSTREAM_URL/v1/chat/completions" '"required"' "${AZURE_AUTH[@]}"
if [ "$STATUS" = "400" ] && [[ "$BODY" == *UnsupportedToolUse* ]]; then
    pass "reproduced: HTTP 400 UnsupportedToolUse"
else
    fail "expected HTTP 400 UnsupportedToolUse, got HTTP $STATUS"
    show_body
fi

echo
echo "=== 3. Direct Azure — tool_choice=\"auto\" (expected: tool_calls) ==="
post "$UPSTREAM_URL/v1/chat/completions" '"auto"' "${AZURE_AUTH[@]}"
if [ "$STATUS" = "200" ] && [ "$(classify)" = "tool_calls" ]; then
    pass "Azure emits native tool_calls with tool_choice=auto"
else
    fail "expected tool_calls, got HTTP $STATUS ($(classify))"
    show_body
fi

echo
echo "=== 4. Via shim — forced function and \"required\" are rewritten to auto ==="
if ! curl -fsS --max-time 3 "$SHIM_URL/healthz" >/dev/null 2>&1; then
    fail "shim not running at $SHIM_URL"
else
    for choice in "$FORCED" '"required"'; do
        post "$SHIM_URL/v1/chat/completions" "$choice"
        if [ "$STATUS" = "200" ] && [ "$(classify)" = "tool_calls" ]; then
            pass "tool_choice=$choice -> native tool_calls"
        else
            fail "tool_choice=$choice: expected tool_calls, got HTTP $STATUS ($(classify))"
            show_body
        fi
    done
fi

echo
if [ "$FAILURES" -eq 0 ]; then
    echo "All checks passed."
else
    echo "$FAILURES check(s) failed."
    exit 1
fi
