# gpt-oss-azure-opencode-shim

> A local HTTP shim for **Azure-hosted GPT-OSS models**. It rewrites the forced `tool_choice` values that Azure AI Foundry rejects, turns answers that miss the required tool call back into that tool call, reports what it did on every request, and keeps the API key out of the client configuration.

[![PyPI](https://img.shields.io/pypi/v/gpt-oss-azure-opencode-shim.svg)](https://pypi.org/project/gpt-oss-azure-opencode-shim/)
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
| OpenCode structured output (10 runs, 95% CI) | 0/10 (0–28%) | **9/10 (60–98%)** |

On the 15-scenario synthetic set the polyfill cut stalled turns from 12% to 4% on 2026-09-22, but a rerun on 2026-09-23 found both arms equal within noise (5% and 7%). The structured-output gain is the one that holds.

Every claim in this README is backed by real requests recorded in [`docs/PROBLEM.md`](https://github.com/Gabrielm3/gpt-oss-azure-opencode-shim/blob/master/docs/PROBLEM.md).

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

**Limit:** `"auto"` lets the model choose. The polyfill only converts answers that already contain schema-valid JSON. A prose answer ("The files are README.md and …") stays as it is, and the request is counted as `failed`. See [`docs/PROBLEM.md`](https://github.com/Gabrielm3/gpt-oss-azure-opencode-shim/blob/master/docs/PROBLEM.md#6-answers-that-miss-the-tool-call).

---

## Quick Start

**1. Install**

```bash
uv tool install gpt-oss-azure-opencode-shim
# or: pipx install gpt-oss-azure-opencode-shim
```

Both put the `gpt-oss-azure-opencode-shim` and `gpt-oss-azure-opencode-shim-report` commands in `~/.local/bin`. Add the `[otel]` extra for OpenTelemetry (`uv tool install 'gpt-oss-azure-opencode-shim[otel]'`). To try it without installing, run `uvx gpt-oss-azure-opencode-shim --help`.

**2. Configure**

The key goes in a file only you can read:

```bash
install -m 600 /dev/null ~/.config/gpt-oss-azure-opencode-shim.env
cat >> ~/.config/gpt-oss-azure-opencode-shim.env <<'EOF'
UPSTREAM_URL=https://YOUR_RESOURCE.services.ai.azure.com/openai
AZURE_FOUNDRY_API_KEY=your-key-here
EOF
```

**3. Run as a systemd user service**

```bash
mkdir -p ~/.config/systemd/user
cat > ~/.config/systemd/user/gpt-oss-azure-opencode-shim.service <<'EOF'
[Unit]
Description=gpt-oss-azure-opencode-shim
After=network.target

[Service]
EnvironmentFile=%h/.config/gpt-oss-azure-opencode-shim.env
ExecStart=%h/.local/bin/gpt-oss-azure-opencode-shim
Restart=always
RestartSec=3

[Install]
WantedBy=default.target
EOF
systemctl --user daemon-reload
systemctl --user enable --now gpt-oss-azure-opencode-shim
curl -s http://127.0.0.1:9526/healthz
```

To upgrade, run `uv tool upgrade gpt-oss-azure-opencode-shim` (or `pipx upgrade gpt-oss-azure-opencode-shim`) and restart the service. From a clone, `./install.sh` does steps 1 to 3 with a local venv instead.

**Or run the container image**

```bash
docker run -d --name gpt-oss-shim --restart unless-stopped \
  -p 127.0.0.1:9526:9526 \
  --env-file ~/.config/gpt-oss-azure-opencode-shim.env \
  --read-only --cap-drop ALL --security-opt no-new-privileges \
  ghcr.io/gabrielm3/gpt-oss-azure-opencode-shim:0.4
```

**Keep `127.0.0.1:` in `-p`.** Inside the container the shim listens on all interfaces (hence the startup warning), so the port mapping is what keeps it local. `-p 9526:9526` would let any machine on your network send requests that the shim signs with your Azure key. Don't set `SHIM_HOST` in the env file for Docker. When another container calls the shim by service name (say `http://shim:9526` in Compose), add that name to `SHIM_ALLOWED_HOSTS`.

The image is multi-arch (amd64, arm64), distroless (no shell), runs as a non-root user and holds the same wheel as the PyPI release. Each release carries an SBOM, build provenance and a signed attestation:

```bash
gh attestation verify oci://ghcr.io/gabrielm3/gpt-oss-azure-opencode-shim:0.4 --owner Gabrielm3
```

**4. Point OpenCode at the shim**

Add to `~/.config/opencode/opencode.json` (or merge with existing — see [`examples/opencode.json`](https://github.com/Gabrielm3/gpt-oss-azure-opencode-shim/blob/master/examples/opencode.json)):

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

[`examples/curl-tests.sh`](https://github.com/Gabrielm3/gpt-oss-azure-opencode-shim/blob/master/examples/curl-tests.sh) reproduces each Azure response and checks the shim against the real deployment:

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

**Tokens, cost and streaming latency.** Every chat answer with HTTP 200 is metered, on every path (plain, streamed, forced and buffered, observe):

| Metric | Meaning |
| ------ | ------- |
| `shim_tokens_total{model,type}` | Tokens Azure reported; `type` is `input` or `output` (output includes reasoning) |
| `shim_cost_usd_total{model}` | Estimated cost: tokens × list price (`SHIM_PRICES`) |
| `shim_usage_missing_total{model}` | Answers that reported no usage, so an undercount is visible |
| `shim_time_to_first_token_seconds{model}` | Passthrough streams: request start to the first reasoning, content or tool-call token |
| `shim_output_tokens_per_second{model}` | Passthrough streams: output tokens from first token to end of stream |

Azure sends `usage` at the end of a stream without `stream_options`, so the shim never changes the request to get it. Tokens are counted even when the client gets a 502 for `choices: []`, because Azure still bills them. Forced requests are buffered, so they have no separate first-token time: `shim_forced_request_duration_seconds` is their latency. The `model` label is the requested model name, capped at 16 values (`other` after that). The cost is an estimate for trends and comparisons: it ignores discounts, cached-input pricing and currency, so the Azure bill remains the source of truth.

### 4. Credentials

The client sends a placeholder key. The shim sends the real key as both `api-key` and `Authorization: Bearer` (Azure accepts either). The key lives only in the shim's environment file, which the Quick Start and `install.sh` create with mode `600`.

**Keyless (Entra ID).** Leave `AZURE_FOUNDRY_API_KEY` unset and install the `[entra]` extra (`uv tool install 'gpt-oss-azure-opencode-shim[entra]'`). The shim then sends an Entra ID bearer token from `DefaultAzureCredential`: an `az login` session, a managed identity, or workload identity federation in CI. Tokens are cached and refreshed 5 minutes before they expire. Your identity needs a data-plane role on the resource, such as `Cognitive Services OpenAI User` or `Foundry User`. If no token can be obtained (for example, an expired `az login`), the request fails with HTTP 502 `upstream_auth_error`, is counted as `upstream_error` in `/metrics`, and the cause is logged. With Entra ID, the resource can run with key auth disabled (`disableLocalAuth`).

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

## Tracing and fixtures

### OpenTelemetry (optional)

```bash
uv tool install 'gpt-oss-azure-opencode-shim[otel]'   # or: pipx install '...[otel]'
OTEL_EXPORTER_OTLP_ENDPOINT=http://127.0.0.1:4318 systemctl --user restart gpt-oss-azure-opencode-shim
```

Each chat request becomes one span named `chat <model>`, with GenAI semantic conventions (`gen_ai.request.model`, `gen_ai.response.model`, `gen_ai.usage.input_tokens`, `gen_ai.usage.output_tokens`, `gen_ai.response.finish_reasons`) plus `shim.outcome`, `shim.polyfill.mode`, `shim.tool_choice.forced` and, in observe mode, `shim.observed_outcome`. A streamed request's span ends with its stream.

The `gen_ai.*` attributes describe the answer as the model produced it, so a rescued answer keeps `finish_reason: "stop"` while `shim.outcome` is `rescued`. **The upstream host is never recorded**, because it contains the Azure resource name. `OTEL_TRACES_EXPORTER=console` prints spans to the log instead of sending them.

### Traces to fixtures

Set `SHIM_TRACE_DIR` and the shim records the forced requests worth replaying (by default `rescued`, `failed` and `empty_choices`, configurable with `SHIM_TRACE_OUTCOMES`):

```bash
SHIM_TRACE_DIR=~/.local/share/gpt-oss-azure-opencode-shim/traces
```

A trace holds the candidate tools, the original `tool_choice` and the raw upstream answer. **It never holds `messages`**, so prompts, file contents and tool results stay out; the directory is created `0700` and the files `0600`. The recorded answer is still model output, so review a trace before committing it.

Traces go to one file per day and are bounded. Each record repeats the tool definitions (36 requests produced 478 KB in testing), so the shim deletes days older than `SHIM_TRACE_RETENTION_DAYS` (default 14) and deletes the oldest days of traces first when the directory passes `SHIM_TRACE_MAX_MB` (default 100); outcome logs leave only through retention. When today's traces alone reach the cap, new traces are dropped until the next day and counted in `shim_traces_dropped_total`. Only files named `traces-YYYY-MM-DD.jsonl` or `outcomes-YYYY-MM-DD.jsonl` are ever deleted.

### Production outcomes

With `SHIM_TRACE_DIR` set, the shim also appends one small line per chat request to `outcomes-YYYY-MM-DD.jsonl`: outcome, polyfill mode, whether the client forced a tool call, HTTP status, latency and model name. It holds no content and is kept even when traces hit the cap. Prometheus counters reset on every restart, while this log gives rates over days:

```bash
gpt-oss-azure-opencode-shim-report ~/.local/share/gpt-oss-azure-opencode-shim/traces --days 7
```

The report shows the share of forced requests and the native, rescued, failed and stalled rates with 95% Wilson intervals. It counts only forced requests that got HTTP 200; upstream errors are listed apart. A second table sums input and output tokens and the estimated cost per UTC day.

Promote one into a regression fixture:

```bash
python -m evals.fixtures list ~/.local/share/gpt-oss-azure-opencode-shim/traces/traces-2026-09-22.jsonl
python -m evals.fixtures promote TRACES.jsonl --line 41 --id reasoning-leak \
  --description "Empty content; the call's arguments end the reasoning" \
  --keep-tools glob,read,StructuredOutput --replace /home/me/project=/repo --coalesce
```

`--keep-tools` drops tools that reveal local setup and `--replace` scrubs strings. Streamed text arrives split across events, so a replacement that still matches the joined text is refused with a pointer to `--coalesce`, which merges the text deltas first. `tests/test_fixture_replay.py` then replays every fixture offline on each CI run: a change in the polyfill that alters a decision on a real answer fails there.

The fixtures in `tests/fixtures/polyfill/` were recorded this way from `gpt-oss-120b` and cover both rescued shapes, the three shapes left unchanged (including a leaked call whose JSON is truncated, where completing it would mean inventing arguments) and two native tool calls.

---

### Dashboard, SLOs and alerts

`observability/` holds a local Prometheus and Grafana stack, with the dashboard, recording rules and alerts versioned as code:

```bash
docker compose -f observability/docker-compose.yml up -d
# Grafana: http://127.0.0.1:3000 (dashboard "gpt-oss shim")   Prometheus: http://127.0.0.1:9090/alerts
```

Both containers use the host network and listen on 127.0.0.1, because the shim accepts only local `Host` headers. The images are pinned by digest and updated by Dependabot.

| Alert | Fires when |
| ----- | ---------- |
| `ShimErrorBudgetFastBurn` (page) | SLO: 99% of requests end without `upstream_error` or `empty_choices`. Burn rate above 14.4× over 1 h and 5 min |
| `ShimErrorBudgetSlowBurn` (ticket) | Burn rate above 6× over 6 h and 30 min |
| `ShimStalledTurnsHigh` | More than 15% of forced requests get no tool call in 1 h (with at least 20 requests); the eval baseline is about 7% |
| `ShimForcedLatencyHigh`, `ShimTimeToFirstTokenHigh` | p95 above 60 s (forced) or 10 s (first token), sustained for 15 min |
| `ShimDailyCostHigh` | Estimated spend above USD 1 in 24 h |
| `ShimUsageMissing` | Answers stop reporting token usage |
| `ShimDown` | Prometheus cannot scrape `/metrics` for 5 min |

The burn-rate pairs follow the multiwindow pattern from the Google SRE workbook. `observability/prometheus/rules.test.yml` runs every alert against synthetic series with `promtool test rules` in CI. The tests use the exact metric names the shim exports, so a rename fails the build. The stack has no Alertmanager: alerts show up on the Prometheus alerts page. To route them, add an Alertmanager with your receiver.

## Evaluation

How the evals run in CI/CD, from PR gates to a nightly drift canary with a statistical gate: [`docs/EVALS.md`](https://github.com/Gabrielm3/gpt-oss-azure-opencode-shim/blob/master/docs/EVALS.md).

[`evals/tool_choice_eval.py`](https://github.com/Gabrielm3/gpt-oss-azure-opencode-shim/blob/master/evals/tool_choice_eval.py) sends 15 forced-tool scenarios ([`evals/scenarios.py`](https://github.com/Gabrielm3/gpt-oss-azure-opencode-shim/blob/master/evals/scenarios.py)) to each target: final-answer steps after a tool result, single-turn extraction into a schema, forced functions, and `"required"` with action tools (7 of them streamed). A run succeeds when the first tool call names the expected tool and its arguments validate against that tool's schema. A turn is stalled when the answer has no tool call at all. For the 9 scenarios with one right answer, the report also checks the argument *values* against golden arguments, split into native and rescued calls. The rescued split is the polyfill's precision ([details](https://github.com/Gabrielm3/gpt-oss-azure-opencode-shim/blob/master/docs/EVALS.md#argument-values-correct-not-just-valid)).

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

Rerun on 2026-09-23 with v0.3.1, which also scores progress (the first tool call is any offered tool with schema-valid arguments, as an agent loop needs) and prints 95% Wilson intervals. Both arms ran from the same checkout in the same session; the rewrite-only arm is the polyfill in `observe` mode, which returns the upstream answer unchanged:

| Target | Strict success | Progress | Stalled turns |
| ------ | -------------- | -------- | ------------- |
| Rewrite only (`observe`) | 55/60 (92%, CI 82–96%) | 55/60 (92%, CI 82–96%) | 3/60 (5%, CI 2–14%) |
| Rewrite + polyfill (`on`) | 53/60 (88%, CI 78–94%) | 56/60 (93%, CI 84–97%) | 4/60 (7%, CI 3–16%) |

On this day the two arms are the same within noise: the intervals overlap on every metric, and the polyfill rescued 2 turns out of 60. The rewrite-only arm stalled on 5% of turns against 12% the day before, with the same scenarios and shim logic, so the upstream model's behavior varies from day to day by more than the polyfill changes it on this set. The synthetic set does not show a polyfill gain; the OpenCode structured-output result above does. Production rates will come from the outcome log (see [Production outcomes](#production-outcomes)).

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
| `AZURE_FOUNDRY_API_KEY` | no       | —           | Azure resource key; unset = Entra ID (`[entra]`)     |
| `SHIM_HOST`             | no       | `127.0.0.1` | Bind address                                         |
| `SHIM_PORT`             | no       | `9526`      | Bind port                                            |
| `SHIM_LOG_LEVEL`        | no       | `info`      | Log level for the shim and uvicorn                   |
| `SHIM_ALLOWED_HOSTS`    | no       | —           | Extra `Host` names to accept, comma-separated        |
| `SHIM_CONNECT_TIMEOUT`  | no       | `10`        | Seconds to open a connection to Azure                |
| `SHIM_READ_TIMEOUT`     | no       | `600`       | Maximum seconds between bytes received from Azure    |
| `SHIM_TOOL_POLYFILL`    | no       | `on`        | `on`, `observe` or `off` (see Tool-call polyfill)    |
| `SHIM_PRICES`           | no       | built in    | `model=input/output` USD per 1M tokens, comma-separated |
| `SHIM_TRACE_DIR`        | no       | —           | Directory for traces of forced requests              |
| `SHIM_TRACE_OUTCOMES`   | no       | `rescued,failed,empty_choices` | Outcomes worth tracing            |
| `SHIM_TRACE_MAX_MB`     | no       | `100`       | Size cap for the trace directory                     |
| `SHIM_TRACE_RETENTION_DAYS` | no   | `14`        | Days of traces and outcome lines to keep             |

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

### Releasing

Releases go to PyPI through [trusted publishing](https://docs.pypi.org/trusted-publishers/): no API token is stored anywhere, and every file carries a PEP 740 attestation.

1. Bump `version` in `pyproject.toml` (the only place it lives) and merge.
2. Tag and push: `git tag -a v1.2.3 -m v1.2.3 && git push origin v1.2.3`.
3. [`release.yml`](https://github.com/Gabrielm3/gpt-oss-azure-opencode-shim/blob/master/.github/workflows/release.yml) builds once, checks the tag against the version, runs [`scripts/check-dist.sh`](https://github.com/Gabrielm3/gpt-oss-azure-opencode-shim/blob/master/scripts/check-dist.sh) (metadata, wheel in a clean venv, full tests from the sdist), then waits for approval on the `pypi` environment before publishing and attaching the files to the GitHub release.

Running the workflow by hand (`gh workflow run release.yml`) is a dry run to TestPyPI. Actions are pinned by commit SHA and build tools by hash, and Dependabot keeps both current.

---

## Contributing

Issues and PRs are welcome. Run `pytest`, `ruff check src tests evals`, and `ruff format --check src tests evals` before opening a PR.

---

## License

MIT — see [`LICENSE`](https://github.com/Gabrielm3/gpt-oss-azure-opencode-shim/blob/master/LICENSE).
