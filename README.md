# gpt-oss-azure-opencode-shim

> A small local HTTP shim for **Azure-hosted GPT-OSS models**. It rewrites the forced `tool_choice` values that Azure AI Foundry rejects, keeps the API key out of the client configuration, and forwards everything else to Azure.

[![CI](https://github.com/Gabrielm3/gpt-oss-azure-opencode-shim/actions/workflows/ci.yml/badge.svg)](https://github.com/Gabrielm3/gpt-oss-azure-opencode-shim/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/downloads/)

---

## TL;DR

Azure AI Foundry deployments of `gpt-oss-120b` do not support a forced tool choice:

| Request | Azure response |
| ------- | -------------- |
| `tool_choice: {"type": "function", ...}` | HTTP 200 with `choices: []`. No error, no output. |
| Same request with `stream: true` | HTTP 200 stream with one error event: `DFLASH speculative decoding does not support grammar-constrained decoding yet.` |
| `tool_choice: "required"` | HTTP 400 `UnsupportedToolUse` |

OpenCode sends `tool_choice: "required"` for structured output (`format: {"type": "json_schema"}`), and other clients force a function to get a guaranteed tool call. The shim rewrites every `tool_choice` other than `"auto"` or `"none"` to `"auto"` before the request reaches Azure.

Every claim in this README is backed by real requests recorded in [`docs/PROBLEM.md`](docs/PROBLEM.md).

---

## The Problem

A client that forces a tool against `gpt-oss-120b` on Azure AI Foundry gets one of three results:

- **Forced function, non-streaming:** HTTP 200 with an empty `choices` array and 2 tokens of usage. The client sees an empty assistant turn, and the tool never runs.
- **Forced function, streaming:** HTTP 200. The only content is an in-band error event, followed by `[DONE]`. Clients that skip error events see an empty turn.
- **`"required"`:** HTTP 400 `Request included unsupported tool use. tool_choice 'required' is not supported for the model.`

The same request with `tool_choice: "auto"` returns native `tool_calls`. The root cause is on the serving side: a forced tool choice needs grammar-constrained decoding, which the deployment's speculative decoding does not support.

---

## When to Use It

Use the shim when a client **forces** a tool call against this deployment:

- OpenCode structured output (`format: {"type": "json_schema"}` in the `opencode serve` API or SDK), which sends `"required"`.
- AI SDK `toolChoice: { type: "tool", toolName }`, OpenAI SDK `tool_choice={"type": "function", ...}`, or agents that force a final tool.

You do not need it for plain `opencode run`. OpenCode 1.18.31 sends `tool_choice: "auto"` for normal agent turns, and those work against Azure directly with the `@ai-sdk/openai-compatible` provider.

**Limit:** `"auto"` lets the model choose. The shim turns a rejected request into a completed one, but the model can still answer with text instead of the tool call. In testing, OpenCode structured output through the shim completed, and `gpt-oss-120b` answered in plain text, so OpenCode reported `StructuredOutputError`. See [`docs/PROBLEM.md`](docs/PROBLEM.md#3-end-to-end-results-with-opencode).

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

The `apiKey` value is a placeholder. The shim replaces it with the real key.

---

## Verify the Fix

[`examples/curl-tests.sh`](examples/curl-tests.sh) reproduces each Azure response and checks the shim against the real deployment:

```bash
export UPSTREAM_URL=https://YOUR_RESOURCE.services.ai.azure.com/openai
export AZURE_FOUNDRY_API_KEY=your-key-here
./examples/curl-tests.sh
```

Expected output:

```
=== 1. Direct Azure — forced function tool_choice (expected: HTTP 200, choices=[]) ===
[PASS] reproduced: HTTP 200 with choices=[]

=== 2. Direct Azure — tool_choice="required" (expected: HTTP 400 UnsupportedToolUse) ===
[PASS] reproduced: HTTP 400 UnsupportedToolUse

=== 3. Direct Azure — tool_choice="auto" (expected: tool_calls) ===
[PASS] Azure emits native tool_calls with tool_choice=auto

=== 4. Via shim — forced function and "required" are rewritten to auto ===
[PASS] tool_choice={"type":"function","function":{"name":"get_files"}} -> native tool_calls
[PASS] tool_choice="required" -> native tool_calls

All checks passed.
```

The script exits with a non-zero status if any check fails, including authentication errors.

---

## How It Works

```
┌──────────┐         ┌────────────────┐         ┌──────────────────┐
│  client  │ ──────▶ │      shim      │ ──────▶ │  Azure Foundry   │
│ OpenCode │         │ 127.0.0.1:9526 │         │  gpt-oss-120b    │
└──────────┘         └────────────────┘         └──────────────────┘
                            │
                            ├── reject browser and non-local requests
                            ├── rewrite forced tool_choice → "auto"
                            ├── inject the API key
                            └── relay response, headers, and SSE stream
```

### 1. Tool choice rewriting

For `POST .../chat/completions`, any `tool_choice` other than `"auto"` or `"none"` becomes `"auto"`. Each rewrite is logged:

```text
INFO gpt_oss_shim: sanitized request: tool_choice='required' -> 'auto'
```

### 2. Credentials

The client sends a placeholder key. The shim sends the real key as both `api-key` and `Authorization: Bearer` (Azure accepts either). The key lives only in the shim's environment file, which `install.sh` creates with mode `600`.

### 3. Header hygiene

The shim sets its own `Content-Type` and drops the client's copy, plus `Authorization`, `Accept-Encoding`, and hop-by-hop headers. Azure rejects a duplicated `Content-Type` (`application/json,application/json`) with HTTP 400.

### 4. Upstream failures

- One shared HTTP client keeps connections to Azure open between requests.
- Connect timeout 10 s, read timeout 600 s between bytes. A stalled upstream returns HTTP 504 instead of hanging.
- Transport errors return HTTP 502 with an OpenAI-style `error` body.
- If a stream breaks mid-way, the shim ends it with a `data: {"error": ...}` event, so the client reports an error instead of a silent, truncated answer.
- Azure response headers such as `retry-after`, `x-ratelimit-*`, and `x-request-id` reach the client.

---

## Security

The shim adds a real API key to every request it forwards and has no inbound authentication. It is built for a single local user:

- It binds to `127.0.0.1` by default and logs a warning if `SHIM_HOST` is not a loopback address.
- It returns HTTP 403 for requests with an `Origin` header or a `Sec-Fetch-Site` value other than `none`. Web pages cannot use the shim through cross-site requests.
- It returns HTTP 403 if the `Host` header is not `localhost`, `127.0.0.1`, `::1`, or a name in `SHIM_ALLOWED_HOSTS`. This blocks DNS rebinding.

Do not expose the shim on a network interface. The `Host` check does not stop a client on the network that sends `Host: localhost`.

---

## Configuration Reference

| Variable                | Required | Default     | Description                                          |
| ----------------------- | -------- | ----------- | ---------------------------------------------------- |
| `UPSTREAM_URL`          | yes      | —           | Azure Foundry base URL (no `/v1`)                    |
| `AZURE_FOUNDRY_API_KEY` | yes      | —           | Azure resource key                                   |
| `SHIM_HOST`             | no       | `127.0.0.1` | Bind address                                         |
| `SHIM_PORT`             | no       | `9526`      | Bind port                                            |
| `SHIM_LOG_LEVEL`        | no       | `info`      | uvicorn log level                                    |
| `SHIM_ALLOWED_HOSTS`    | no       | —           | Extra `Host` names to accept, comma-separated        |
| `SHIM_CONNECT_TIMEOUT`  | no       | `10`        | Seconds to open a connection to Azure                |
| `SHIM_READ_TIMEOUT`     | no       | `600`       | Maximum seconds between bytes received from Azure    |

---

## Compatibility

| Component     | Tested Version                    |
| ------------- | --------------------------------- |
| Python        | 3.10, 3.11, 3.12, 3.13            |
| OpenCode      | 1.18.31                           |
| Azure GPT-OSS | `gpt-oss-120b` (Chat Completions) |
| OS            | Linux (systemd user service)      |

Likely applies to `gpt-oss-20b` as well (untested).

**Not a protocol translator.** The shim assumes both sides speak OpenAI Chat Completions. Use the `@ai-sdk/openai-compatible` provider in OpenCode. The `@ai-sdk/openai` package failed against the same deployment for reasons unrelated to `tool_choice`.

**Does not fix OpenCode's empty-assistant-materialization bug** when the model returns only reasoning. That's an upstream issue.

---

## Development

```bash
git clone https://github.com/Gabrielm3/gpt-oss-azure-opencode-shim.git
cd gpt-oss-azure-opencode-shim
python3 -m venv .venv
./.venv/bin/pip install -e ".[dev]"
./.venv/bin/pytest -v
./.venv/bin/ruff check src tests
./.venv/bin/ruff format --check src tests
```

---

## Contributing

Issues and PRs are welcome. Run `pytest`, `ruff check src tests`, and `ruff format --check src tests` before opening a PR.

---

## License

MIT — see [`LICENSE`](LICENSE).
