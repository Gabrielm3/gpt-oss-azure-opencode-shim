#!/usr/bin/env bash
#
# Reproduce the Azure GPT-OSS "silent failure" bug and verify the fix.
#
# Requires:
#   UPSTREAM_URL          — Azure AI Foundry base URL
#   AZURE_FOUNDRY_API_KEY — API key
#   SHIM_URL              — (optional) shim URL, default http://127.0.0.1:9526
#
set -euo pipefail

: "${UPSTREAM_URL:?UPSTREAM_URL is required}"
: "${AZURE_FOUNDRY_API_KEY:?AZURE_FOUNDRY_API_KEY is required}"
SHIM_URL="${SHIM_URL:-http://127.0.0.1:9526}"
MODEL="${MODEL:-gpt-oss-120b}"

pass() { echo "[PASS] $*"; }
fail() { echo "[FAIL] $*"; }

# Shared payload pieces
TOOLS='[{"type":"function","function":{"name":"get_files","description":"List files matching a glob.","parameters":{"type":"object","properties":{"pattern":{"type":"string"}},"required":["pattern"]}}}]'

# ---------------------------------------------------------------
# 1. Direct Azure, forced tool_choice — the bug
# ---------------------------------------------------------------
echo
echo "=== 1. Direct Azure — forced tool_choice (expected: choices=[]) ==="
DIRECT=$(curl -sS --max-time 60 \
    -X POST "$UPSTREAM_URL/v1/chat/completions" \
    -H "api-key: $AZURE_FOUNDRY_API_KEY" \
    -H "Content-Type: application/json" \
    -d "{\"model\":\"$MODEL\",\"messages\":[{\"role\":\"user\",\"content\":\"Call get_files.\"}],\"tools\":$TOOLS,\"tool_choice\":{\"type\":\"function\",\"function\":{\"name\":\"get_files\"}},\"stream\":false,\"max_completion_tokens\":2048}")

COUNT=$(printf '%s' "$DIRECT" | python3 -c 'import json,sys; print(len(json.load(sys.stdin).get("choices",[])))')

if [ "$COUNT" = "0" ]; then
    pass "reproduced: Azure returned choices=[] silently"
else
    echo "[INFO] Azure did not reproduce (choices=$COUNT). Response:"
    printf '%s\n' "$DIRECT" | head -c 500
fi

# ---------------------------------------------------------------
# 2. Direct Azure, tool_choice=auto — the fix works
# ---------------------------------------------------------------
echo
echo "=== 2. Direct Azure — tool_choice=auto (expected: tool_calls) ==="
DIRECT_AUTO=$(curl -sS --max-time 60 \
    -X POST "$UPSTREAM_URL/v1/chat/completions" \
    -H "api-key: $AZURE_FOUNDRY_API_KEY" \
    -H "Content-Type: application/json" \
    -d "{\"model\":\"$MODEL\",\"messages\":[{\"role\":\"system\",\"content\":\"You MUST call get_files.\"},{\"role\":\"user\",\"content\":\"List markdown files with pattern **/*.md.\"}],\"tools\":$TOOLS,\"tool_choice\":\"auto\",\"stream\":false,\"max_completion_tokens\":2048}")

HAS_TC=$(printf '%s' "$DIRECT_AUTO" | python3 -c 'import json,sys; d=json.load(sys.stdin); print(1 if (d.get("choices") and d["choices"][0]["message"].get("tool_calls")) else 0)')

if [ "$HAS_TC" = "1" ]; then
    pass "Azure emits native tool_calls with tool_choice=auto"
else
    echo "[INFO] No tool_calls in response:"
    printf '%s\n' "$DIRECT_AUTO" | head -c 500
fi

# ---------------------------------------------------------------
# 3. Shim — same forced tool_choice is rewritten and works
# ---------------------------------------------------------------
echo
echo "=== 3. Via shim — forced tool_choice is rewritten to auto ==="
if ! curl -fsS --max-time 3 "$SHIM_URL/healthz" >/dev/null 2>&1; then
    echo "[SKIP] shim not running at $SHIM_URL"
    exit 0
fi

VIA_SHIM=$(curl -sS --max-time 60 \
    -X POST "$SHIM_URL/v1/chat/completions" \
    -H "Content-Type: application/json" \
    -d "{\"model\":\"$MODEL\",\"messages\":[{\"role\":\"system\",\"content\":\"You MUST call get_files.\"},{\"role\":\"user\",\"content\":\"List markdown files with pattern **/*.md.\"}],\"tools\":$TOOLS,\"tool_choice\":{\"type\":\"function\",\"function\":{\"name\":\"get_files\"}},\"stream\":false,\"max_completion_tokens\":2048}")

SHIM_TC=$(printf '%s' "$VIA_SHIM" | python3 -c 'import json,sys; d=json.load(sys.stdin); print(1 if (d.get("choices") and d["choices"][0]["message"].get("tool_calls")) else 0)')

if [ "$SHIM_TC" = "1" ]; then
    pass "shim rewrote forced tool_choice; native tool_calls received"
else
    fail "shim response did not contain tool_calls"
    printf '%s\n' "$VIA_SHIM" | head -c 500
    exit 1
fi

echo
echo "All checks done."
