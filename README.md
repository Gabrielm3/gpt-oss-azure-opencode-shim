# gpt-oss-azure-opencode-shim

> A lightweight compatibility shim that makes **Azure-hosted GPT-OSS models** work with **OpenCode** by fixing silent API incompatibilities.

[![CI](https://github.com/Gabrielm3/gpt-oss-azure-opencode-shim/actions/workflows/ci.yml/badge.svg)](https://github.com/Gabrielm3/gpt-oss-azure-opencode-shim/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/downloads/)

---

## TL;DR

When OpenCode talks to Azure AI Foundry deployments of GPT-OSS, three silent failures can make tool-calling hang forever:

1. **Azure expects `api-key`**, but OpenAI-compatible SDKs send `Authorization: Bearer`.
2. **Duplicated `Content-Type`** headers (`application/json,application/json`) get rejected.
3. **Azure silently ignores forced `tool_choice`**, returning `choices: []` with HTTP 200.

The shim sits between OpenCode and Azure, applies three small fixes, and forwards everything else unchanged. ~250 lines of Python. Configuration: two environment variables plus one OpenCode provider entry.

---

## The Problem

You're using an OpenAI-compatible client (OpenCode, in this case) pointed at an Azure AI Foundry deployment of `gpt-oss-120b`. Direct `curl` requests work. But `opencode run` hangs, drops tool calls, or returns empty assistant messages.

```
OpenCode  ──▶  Azure Foundry  ──▶  silent failure
```

The client never sees an error — just an empty response. Common symptoms:

- `opencode run "..."` exits with code 0 but prints nothing.
- Tool calling not working: tool calls (Glob, Read, Bash) never fire.
- Assistant turns persist with `finish: "stop"` and zero parts.
- `curl` to the same endpoint returns a valid response.

This is not a network, auth, or model problem. It's a **protocol dialect mismatch** — the client and the server both speak "OpenAI Chat Completions", but disagree on three details.

---

## Quick Start

**1. Install**

```bash
git clone https://github.com/Gabrielm3/gpt-oss-azure-opencode-shim.git
cd gpt-oss-azure-opencode-shim
./install.sh
```

**2. Configure**

Edit `~/.config/gpt-oss-azure-opencode-shim.env`:

```bash
UPSTREAM_URL=https://YOUR_RESOURCE.services.ai.azure.com/openai
AZURE_FOUNDRY_API_KEY=your-key-here
```

**3. Run**

```bash
systemctl --user enable --now gpt-oss-azure-opencode-shim
```

**4. Point OpenCode at the shim**

Add to `~/.config/opencode/opencode.json` (or merge with existing — see [`examples/opencode.json`](examples/opencode.json)):

```json
{
  "provider": {
    "azure-gpt-oss": {
      "npm": "@ai-sdk/openai-compatible",
      "name": "Azure GPT-OSS via shim",
      "options": {
        "baseURL": "http://127.0.0.1:9526/v1",
        "apiKey": "shim"
      },
      "models": {
        "gpt-oss-120b": {
          "name": "GPT-OSS 120B",
          "reasoning": false,
          "limit": { "context": 131072, "output": 32768 }
        }
      }
    }
  }
}
```

`opencode run -m azure-gpt-oss/gpt-oss-120b "..."` now completes tool calls.

---

## Verify the Fix

[`examples/curl-tests.sh`](examples/curl-tests.sh) reproduces the bug and confirms the fix in one script:

```bash
export UPSTREAM_URL=https://YOUR_RESOURCE.services.ai.azure.com/openai
export AZURE_FOUNDRY_API_KEY=your-key-here
./examples/curl-tests.sh
```

Expected output:

```
=== 1. Direct Azure — forced tool_choice (expected: choices=[]) ===
[PASS] reproduced: Azure returned choices=[] silently

=== 2. Direct Azure — tool_choice=auto (expected: tool_calls) ===
[PASS] Azure emits native tool_calls with tool_choice=auto

=== 3. Via shim — forced tool_choice is rewritten to auto ===
[PASS] shim rewrote forced tool_choice; native tool_calls received
```

---

## How It Works

```
┌──────────┐         ┌────────────────┐         ┌──────────────────┐
│ OpenCode │ ──────▶ │      shim      │ ──────▶ │  Azure Foundry   │
│  :local  │         │ 127.0.0.1:9526 │         │  gpt-oss-120b    │
└──────────┘         └────────────────┘         └──────────────────┘
                            │
                            ├── inject api-key + Bearer
                            ├── strip duplicate Content-Type
                            └── rewrite forced tool_choice → "auto"
```

The shim is a transparent FastAPI proxy. On every request, it applies three fixes:

### 1. Authentication

Azure AI Foundry uses the `api-key` header. OpenAI-compatible SDKs use `Authorization: Bearer`. The shim sends **both**:

```python
{
    "api-key": AZURE_FOUNDRY_API_KEY,
    "Authorization": f"Bearer {AZURE_FOUNDRY_API_KEY}",
}
```

### 2. Header hygiene

If the client sets `Content-Type` and the shim also sets it, upstream receives `application/json,application/json` and rejects the request. The shim strips client-provided `Content-Type`, `Accept-Encoding`, `Authorization`, and hop-by-hop headers before forwarding.

### 3. Tool choice rewriting

This is the core fix. Azure GPT-OSS supports only `tool_choice: "auto"` and `"none"`. Any other value — a dict like `{"type":"function",...}` or the string `"required"` — triggers **silent rejection**:

```http
HTTP/1.1 200 OK
{ "choices": [] }   ← empty
```

The client interprets this as "the model decided not to call anything" and hangs. The shim rewrites every unsafe `tool_choice` to `"auto"`:

```python
choice = payload.get("tool_choice")
if choice is not None and not (isinstance(choice, str) and choice in {"auto", "none"}):
    payload["tool_choice"] = "auto"
```

---

## Why This Exists

While integrating GPT-OSS on Azure with OpenCode for agentic coding, tool calling would hang indefinitely. `curl` worked, but the CLI didn't. Debugging revealed three stacked protocol mismatches — none of them documented, all of them silent.

The shim isolates the workaround in one place, with no changes to OpenCode or the Azure SDK. The root cause was isolated by inspecting raw SSE streams, OpenCode's SQLite session state, and changing one request field at a time.

---

## Configuration Reference

| Variable                | Required | Default     | Description                       |
| ----------------------- | -------- | ----------- | --------------------------------- |
| `UPSTREAM_URL`          | yes      | —           | Azure Foundry base URL (no `/v1`) |
| `AZURE_FOUNDRY_API_KEY` | yes      | —           | Azure resource key                |
| `SHIM_HOST`             | no       | `127.0.0.1` | Bind address                      |
| `SHIM_PORT`             | no       | `9526`      | Bind port                         |
| `SHIM_LOG_LEVEL`        | no       | `info`      | uvicorn log level                 |

---

## Compatibility

| Component     | Tested Version                    |
| ------------- | --------------------------------- |
| Python        | 3.10, 3.11, 3.12, 3.13            |
| OpenCode      | 1.18.x                            |
| Azure GPT-OSS | `gpt-oss-120b` (Chat Completions) |
| OS            | Linux (systemd user service)      |

Likely applies to `gpt-oss-20b` as well (untested).

**Not a proxy for translating between different protocols.** The shim assumes both sides speak OpenAI Chat Completions. It only fixes dialect mismatches.

**Does not fix OpenCode's empty-assistant-materialization bug** when the model returns only reasoning. That's an upstream issue.

---

## Development

```bash
git clone https://github.com/Gabrielm3/gpt-oss-azure-opencode-shim.git
cd gpt-oss-azure-opencode-shim
python3 -m venv .venv
./.venv/bin/pip install -e ".[dev]"
./.venv/bin/pytest -v        # 16 tests
./.venv/bin/ruff check src tests
```

---

## Contributing

Issues and PRs are welcome. Run `pytest` and `ruff check src tests` before opening a PR.

---

## License

MIT — see [`LICENSE`](LICENSE).
