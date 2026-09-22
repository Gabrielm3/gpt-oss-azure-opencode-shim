# gpt-oss-azure-opencode-shim

> A small local HTTP shim for **Azure-hosted GPT-OSS models**. It rewrites the forced `tool_choice` values that Azure AI Foundry rejects, turns answers that miss the required tool call back into that tool call, reports what it did on every request, and keeps the API key out of the client configuration.

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

With `"auto"`, the model sometimes does the work but misses the tool call: it writes the answer as JSON text, or leaks the call's arguments into its reasoning and returns an empty turn. For forced requests, the shim checks the answer against the tool schemas and returns the tool call when exactly one tool matches.

Measured against the real deployment (see [Evaluation](#evaluation)):

| | Rewrite only (v0.1.1) | Rewrite + polyfill (v0.2.0) |
| --- | --- | --- |
| OpenCode structured output (10 runs) | 0/10 | **9/10** |
| Stalled turns on forced requests (no tool call at all; upstream HTTP 500s excluded) | 7/59 (12%) | **2/56 (4%)** |

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

**Limit:** `"auto"` lets the model choose. The polyfill only converts answers that already contain schema-valid JSON. A prose answer ("The files are README.md and …") stays as it is, and the request is counted as `failed`. See [`docs/PROBLEM.md`](docs/PROBLEM.md#6-answers-that-miss-the-tool-call).

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
                            ├── forced requests: check answer, repair tool call
                            ├── relay response, headers, and SSE stream
                            └── x-shim-outcome header + /metrics
```

### 1. Tool choice rewriting

For `POST .../chat/completions`, any `tool_choice` other than `"auto"` or `"none"` becomes `"auto"`. Each rewrite is logged:

```text
INFO gpt_oss_shim: sanitized request: tool_choice='required' -> 'auto'
```

### 2. Tool-call polyfill

For a forced request, the shim buffers the answer (streamed or not) and inspects it:

- The model called a tool: the answer passes unchanged (`native`).
- The answer text is a JSON object, with or without a Markdown fence: it becomes a tool call.
- The answer text is empty and the reasoning ends with a JSON object: it becomes a tool call. This is how gpt-oss leaks a call it did not emit.
- Anything else passes unchanged (`failed`).

Matching is strict. The JSON must validate against the tool's JSON Schema and use only declared top-level properties, and exactly one candidate tool may match (only the named tool for a forced function). The shim never guesses. For a rescued stream, the reasoning and usage events are kept, and the answer text is replaced by one tool-call chunk with `finish_reason: "tool_calls"`.

Only forced requests are buffered, so other requests keep streaming token by token.

`SHIM_TOOL_POLYFILL` selects the mode:

| Mode | Forced answers | Buffering | Use it to |
| ---- | -------------- | --------- | --------- |
| `on` (default) | Repaired into the tool call when possible | Yes, forced requests only | Get the tool call |
| `observe` | Returned unchanged; the shim evaluates a copy after the stream ends and counts what a repair would have done in `shim_polyfill_observed_total{outcome}` | No | Measure the impact on your own traffic before turning the repair on |
| `off` | Returned unchanged, not evaluated | No | Keep only the `tool_choice` rewrite (v0.1.1 behavior) |

### 3. Outcomes and metrics

Every forwarded request gets an `x-shim-outcome` response header:

| Outcome | Meaning |
| ------- | ------- |
| `passthrough` | Nothing forced, nothing changed |
| `rewritten` | Forced `tool_choice` rewritten; answer not inspected (polyfill off, or upstream error status) |
| `native` | Forced; the model called a tool itself |
| `rescued` | Forced; the polyfill turned the answer into the tool call |
| `failed` | Forced; no tool call and nothing to convert |
| `empty_choices` | Azure answered HTTP 200 with `choices: []`; the shim returns HTTP 502 instead |
| `upstream_error` | Transport error or timeout (HTTP 502 or 504) |

`GET /metrics` exposes the same outcomes in Prometheus format:

```text
shim_requests_total{outcome="native"} 59.0
shim_requests_total{outcome="rescued"} 13.0
shim_requests_total{outcome="failed"} 3.0
shim_forced_request_duration_seconds_sum{outcome="rescued"} 15.63
shim_forced_request_duration_seconds_count{outcome="rescued"} 13.0
```

`shim_forced_request_duration_seconds` is the time a forced request waits before its first byte, because the shim buffers it. In `observe` mode, `shim_polyfill_observed_total{outcome}` counts what the repair would have done while every answer stays unchanged (`x-shim-outcome: rewritten`). `native_rate = native / forced` also shows when Azure starts supporting forced tool choice, which is when the polyfill stops being needed.

### 4. Credentials

The client sends a placeholder key. The shim sends the real key as both `api-key` and `Authorization: Bearer` (Azure accepts either). The key lives only in the shim's environment file, which `install.sh` creates with mode `600`.

### 5. Header hygiene

The shim sets its own `Content-Type` and drops the client's copy, plus `Authorization`, `Accept-Encoding`, and hop-by-hop headers. Azure rejects a duplicated `Content-Type` (`application/json,application/json`) with HTTP 400.

### 6. Upstream failures

- One shared HTTP client keeps connections to Azure open between requests.
- Connect timeout 10 s, read timeout 600 s between bytes. A stalled upstream returns HTTP 504 instead of hanging.
- Transport errors return HTTP 502 with an OpenAI-style `error` body.
- If a stream breaks mid-way, the shim ends it with a `data: {"error": ...}` event, so the client reports an error instead of a silent, truncated answer.
- Azure response headers such as `retry-after`, `x-ratelimit-*`, and `x-request-id` reach the client.
- Logs never contain the Azure resource name: the HTTP client's request lines stay hidden unless `SHIM_LOG_LEVEL=debug`.

---

## Evaluation

[`evals/tool_choice_eval.py`](evals/tool_choice_eval.py) sends 15 forced-tool scenarios ([`evals/scenarios.py`](evals/scenarios.py)) to each target: final-answer steps after a tool result, single-turn extraction into a schema, forced functions, and `"required"` with action tools (7 of them streamed). A run succeeds when the first tool call names the expected tool and its arguments validate against that tool's schema. A turn is stalled when the answer has no tool call at all.

Results on 2026-09-22, `gpt-oss-120b`, 15 scenarios × 4 repetitions per target:

| Target | Strict success | Stalled turns | Outcomes | p50 / p95 |
| ------ | -------------- | ------------- | -------- | --------- |
| Direct to Azure | 0/60 (0%) | 16/16 (100%) | 43 × HTTP 400 | 0.2 / 0.3 s |
| Rewrite only (v0.1.1) | 50/60 (83%) | 7/59 (12%) | rewritten 60 | 0.5 / 0.9 s |
| Rewrite + polyfill (v0.2.0) | 49/60 (82%) | 2/56 (4%) | native 50, rescued 4, failed 2, rewritten 4 (HTTP 500) | 0.5 / 0.9 s |

What the numbers show:

- Strict success is the same within noise on this synthetic set. The 4 rescued turns were calls to an intermediate tool (`read`, `bash`) that the model had leaked into its reasoning, while the scenario expected the final `StructuredOutput`. In an agent loop that is progress, not a stall, but the strict scorer counts it as a miss.
- The polyfill cuts stalled turns from 12% to 4%.
- Azure returned HTTP 500 to 6 requests spread over all three targets. Those are upstream errors, not shim failures.

End-to-end with OpenCode 1.18.31 structured output (`format: json_schema`, 10 runs each, same session):

| Target | Structured output returned | Median latency |
| ------ | -------------------------- | -------------- |
| Rewrite only (v0.1.1) | 0/10 (`StructuredOutputError` every time) | 2.9 s |
| Rewrite + polyfill (v0.2.0) | 9/10 | 3.2 s |

OpenCode's structured-output prompt makes the model write the answer as JSON, which the polyfill can convert. That is why the gain is large here and small on the synthetic set.

Run it yourself (costs a few cents of tokens):

```bash
python -m evals.tool_choice_eval \
  --target direct=$UPSTREAM_URL/v1/chat/completions \
  --target shim=http://127.0.0.1:9526/v1/chat/completions \
  --repeat 4 --out eval-results.json
```

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
| `SHIM_LOG_LEVEL`        | no       | `info`      | Log level for the shim and uvicorn                   |
| `SHIM_ALLOWED_HOSTS`    | no       | —           | Extra `Host` names to accept, comma-separated        |
| `SHIM_CONNECT_TIMEOUT`  | no       | `10`        | Seconds to open a connection to Azure                |
| `SHIM_READ_TIMEOUT`     | no       | `600`       | Maximum seconds between bytes received from Azure    |
| `SHIM_TOOL_POLYFILL`    | no       | `on`        | `on`, `observe` or `off` (see Tool-call polyfill)    |

---

## Compatibility

| Component     | Tested Version                    |
| ------------- | --------------------------------- |
| Python        | 3.10, 3.11, 3.12, 3.13            |
| OpenCode      | 1.18.31                           |
| Azure GPT-OSS | `gpt-oss-120b` (Chat Completions) |
| OS            | Linux (systemd user service)      |

Likely applies to `gpt-oss-20b` as well (untested).

**Not a protocol translator.** The shim assumes both sides speak OpenAI Chat Completions. Use the `@ai-sdk/openai-compatible` provider in OpenCode. The `@ai-sdk/openai` package failed against the same deployment with `Invalid parameter: the model does not support one or more of the provided input parameters`, which this shim does not address.

**Does not fix OpenCode's empty-assistant-materialization bug** when the model returns only reasoning. That's an upstream issue.

---

## Development

```bash
git clone https://github.com/Gabrielm3/gpt-oss-azure-opencode-shim.git
cd gpt-oss-azure-opencode-shim
python3 -m venv .venv
./.venv/bin/pip install -e ".[dev]"
./.venv/bin/pytest -v
./.venv/bin/ruff check src tests evals
./.venv/bin/ruff format --check src tests evals
```

---

## Contributing

Issues and PRs are welcome. Run `pytest`, `ruff check src tests evals`, and `ruff format --check src tests evals` before opening a PR.

---

## License

MIT — see [`LICENSE`](LICENSE).
