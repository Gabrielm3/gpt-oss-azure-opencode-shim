# Forced `tool_choice` on Azure-hosted GPT-OSS

This document records how an Azure AI Foundry deployment of `gpt-oss-120b`
responds to each form of `tool_choice`, where OpenCode sends a forced value,
and what the shim changes. Every result below comes from real requests. The
resource name, API key, and request IDs are removed.

- Date: 2026-09-21
- Model: `gpt-oss-120b`, Chat Completions at `https://YOUR_RESOURCE.services.ai.azure.com/openai/v1/chat/completions`
- Client: OpenCode 1.18.31 with the `@ai-sdk/openai-compatible` provider
- Request body for the direct tests: one `get_files` function tool, a system
  message that says "You MUST call get_files.", `max_completion_tokens: 1024`

## 1. Direct requests to Azure

| # | Request | Result |
|---|---------|--------|
| E1 | `tool_choice: {"type": "function", "function": {"name": "get_files"}}` | HTTP 200, `choices: []`, usage `1/1/2` tokens |
| E2 | `tool_choice: "required"` | HTTP 400, `UnsupportedToolUse` |
| E3 | `tool_choice: "auto"` | HTTP 200, `finish_reason: "tool_calls"`, calls `get_files` |
| E3b | `tool_choice` omitted | HTTP 200, `finish_reason: "tool_calls"`, calls `get_files` |
| E4 | E1 with `stream: true` | HTTP 200 SSE: one error event, one chunk with `choices: []`, `[DONE]` |
| E5 | E3 authenticated with `Authorization: Bearer <key>` only (no `api-key`) | HTTP 200, calls `get_files` |
| E6 | E3 with the `Content-Type` header sent twice | HTTP 400, `invalid_header` |

### E1: forced function, non-streaming

The request succeeds at the HTTP level and returns no choice. The response
carries no error:

```json
{"id":"…","model":"gpt-oss-120b","choices":[],"usage":{"prompt_tokens":1,"completion_tokens":1,"total_tokens":2,"audio_prompt_tokens":0},"created":…,"object":"chat.completion","prompt_filter_results":null}
```

A client that trusts HTTP 200 sees an assistant turn with no content and no
tool call.

### E4: forced function, streaming — the root cause

With `stream: true`, the upstream error is visible inside the stream:

```text
data: {"error": {"object": "error", "message": "DFLASH speculative decoding does not support grammar-constrained decoding yet.", "type": "BAD_REQUEST", "param": null, …}}
data: {"id":"…","object":"chat.completion.chunk","model":"gpt-oss-120b","choices":[],"usage":{"prompt_tokens":1,…}}
data: [DONE]
```

A forced tool choice needs grammar-constrained decoding, and the serving
backend of this deployment does not support it together with speculative
decoding. Without streaming (E1), the same error is dropped and only
`choices: []` remains.

### E2: `"required"`

```json
{"error":{"code":"UnsupportedToolUse","message":"Request included unsupported tool use. tool_choice 'required' is not supported for the model.","details":"…"}}
```

This failure is explicit: HTTP 400 with a clear message.

### E5 and E6: authentication and headers

- Azure accepts the key as `Authorization: Bearer <key>` as well as
  `api-key: <key>`. A client does not need a header translation to
  authenticate.
- Azure rejects a duplicated `Content-Type` with HTTP 400:

  ```json
  {"error":{"code":"invalid_header","message":"Header 'Content-Type' has invalid value `application/json,application/json`","details":"…"}}
  ```

  A single client does not send it twice. It happens when a proxy adds its own
  `Content-Type` on top of the client's, which is why the shim removes the
  client's copy before it adds one.

## 2. Where OpenCode sends a forced `tool_choice`

Requests from OpenCode 1.18.31 were recorded with a logging pass-through
proxy:

| OpenCode action | `tool_choice` sent | Tools |
|-----------------|--------------------|-------|
| Session title generation | not sent | 0 |
| `opencode run "…"` agent turns | `"auto"` | 12 |
| Prompt with `format: {"type": "json_schema", …}` (structured output, `opencode serve` API) | `"required"` | 14, including `StructuredOutput` |

The shipped OpenCode bundle contains the matching logic in its session loop:
`toolChoice: format.type === "json_schema" ? "required" : undefined`.

OpenCode requests carry no `Origin` or `Sec-Fetch-*` headers, and their `Host`
is the loopback address of the provider `baseURL`.

## 3. End-to-end results with OpenCode

| Scenario | Direct to Azure | Shim v0.1.1 (rewrite only) | Shim v0.2.0 (rewrite + polyfill) |
|----------|-----------------|----------------------------|----------------------------------|
| `opencode run` with a tool call (Glob) | Works | Works | Works |
| Structured output (`format: json_schema`), 10 runs | `APIError`: `tool_choice 'required' is not supported for the model` (HTTP 400) | 0/10: `StructuredOutputError: Model did not produce structured output` | 9/10 structured output returned |

Conclusions:

1. With OpenCode 1.18.31 and the `@ai-sdk/openai-compatible` provider, normal
   agent runs work against Azure without the shim.
2. Rewriting to `"auto"` alone replaces the HTTP 400 with a completed request,
   but the model then answers without calling `StructuredOutput`. Section 6
   shows what it writes instead, and how v0.2.0 recovers the tool call.

A separate note: `opencode run` with the `@ai-sdk/openai` provider package
failed against the same deployment with `Invalid parameter: the model does
not support one or more of the provided input parameters`. The cause was not
isolated, and the shim does not address it. Use `@ai-sdk/openai-compatible`.

## 4. Requests through the shim

The same forced requests, sent to the shim with no credentials:

| Request | Result |
|---------|--------|
| Forced function, non-streaming | HTTP 200, `finish_reason: "tool_calls"`, calls `get_files` |
| `"required"`, non-streaming | HTTP 200, `finish_reason: "tool_calls"`, calls `get_files` |
| Forced function, `stream: true` | HTTP 200 SSE, streamed `get_files` call, ends with `[DONE]` |

The shim logs each rewrite, for example:

```text
INFO gpt_oss_shim: sanitized request: tool_choice='required' -> 'auto'
```

Azure response headers (`x-request-id`, `apim-request-id`,
`x-ratelimit-remaining-*`) reach the client unchanged.

## 5. Reproduce

```bash
export UPSTREAM_URL=https://YOUR_RESOURCE.services.ai.azure.com/openai
export AZURE_FOUNDRY_API_KEY=your-key-here
./examples/curl-tests.sh
```

The script checks E1, E2 and E3 directly against Azure, then checks the
forced-function and `"required"` cases through the shim. It exits with a
non-zero status if any result differs from the tables above.

## 6. Answers that miss the tool call

With the rewrite to `"auto"`, `gpt-oss-120b` usually calls the tool. When it
does not, the answer takes one of three shapes. Each one below was recorded
from real responses to forced requests:

| Shape | Example | Shim v0.2.0 |
|-------|---------|-------------|
| The answer as JSON text | content `{"files": ["beta.md", "alpha.md"]}`, `finish_reason: "stop"` | Converted into the tool call |
| The call leaked into the reasoning, empty content | reasoning `…Let's open whole file.{"filePath": "pyproject.toml"}`, content `""`, `finish_reason: "stop"` | Converted into the tool call |
| A prose answer | `The directory contains the following Markdown files: …` | Left unchanged (`failed`) |

The first shape is what OpenCode structured output produced every time
(section 3): OpenCode asks for JSON, and the model writes the JSON as text
instead of passing it to `StructuredOutput`. The second shape is what clients
see as an empty assistant turn.

The polyfill converts an answer only when the JSON validates against the
tool's JSON Schema, uses only declared top-level properties, and matches
exactly one candidate tool. It never guesses a tool for prose.

### Two things that did not help

Before the polyfill, two cheaper ideas were tested on OpenCode structured
output (5 runs each). Neither returned any structured output:

| Attempt | Result |
|---------|--------|
| Add an instruction to the system prompt: call one of the tools, do not answer in plain text | 0/5 |
| OpenCode `format.retryCount: 3` | 0/5, and no retry was observed (same latency as without it) |

With `retryCount` set, `GET /session/{id}/message` on `opencode serve`
1.18.31 fails with HTTP 400 `Expected OutputFormatJsonSchema`, so the session
can no longer be read through the API.

### Evaluation

`evals/tool_choice_eval.py` runs 15 forced-tool scenarios against each target
(15 × 4 runs per target, 2026-09-22):

| Target | Strict success | Stalled turns (no tool call) |
|--------|----------------|------------------------------|
| Direct to Azure | 0/60 | 16/16 |
| Shim v0.1.1 (rewrite only) | 50/60 | 7/59 |
| Shim v0.2.0 (rewrite + polyfill) | 49/60 | 2/56 |

Strict success counts only the expected tool with schema-valid arguments. The
polyfill rescued 4 turns there, all calls to an intermediate tool the model had
leaked into its reasoning, so the strict scorer did not count them. Azure
answered 6 requests, spread over all three targets, with HTTP 500. Those are
excluded from the stalled-turn ratio.

Rerun on 2026-09-23 with v0.3.1 (15 × 4 runs per arm, both arms from the same
checkout at the same time; rewrite-only is `SHIM_TOOL_POLYFILL=observe`, which
leaves answers unchanged). Rates with 95% Wilson intervals:

| Target | Strict success | Progress (any offered tool, valid arguments) | Stalled turns |
|--------|----------------|----------------------------------------------|---------------|
| Rewrite only (observe) | 55/60 (82–96%) | 55/60 (82–96%) | 3/60 (2–14%) |
| Rewrite + polyfill (on) | 53/60 (78–94%) | 56/60 (84–97%) | 4/60 (3–16%) |

Polyfill outcomes: native 54, rescued 2, failed 4. Strict failures on the
polyfill arm: no tool call 4, a valid call to `read` where `StructuredOutput`
was expected 3. In observe mode the shim counted what a repair would have done:
native 57, failed 3, rescued 0. No upstream errors this time.

The two arms are equal within noise on this set. The rewrite-only stall rate
fell from 12% to 5% between the two days with no change to scenarios or
rewrite logic, which is upstream variation.
