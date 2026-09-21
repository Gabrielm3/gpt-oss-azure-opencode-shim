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

| Scenario | Direct to Azure | Through the shim |
|----------|-----------------|------------------|
| `opencode run` with a tool call (Glob) | Works | Works |
| Structured output (`format: json_schema`) | `APIError`: `tool_choice 'required' is not supported for the model` (HTTP 400) | The request completes. `gpt-oss-120b` answered in plain text, so OpenCode reported `StructuredOutputError: Model did not produce structured output` |

Two conclusions:

1. With OpenCode 1.18.31 and the `@ai-sdk/openai-compatible` provider, normal
   agent runs work against Azure without the shim.
2. For structured output, the shim replaces the HTTP 400 with a completed
   request. Rewriting to `"auto"` removes the constraint, so the model can
   still answer without calling `StructuredOutput`. The shim cannot force
   schema-conforming output.

A separate note: the `@ai-sdk/openai` provider package failed against the
same deployment with `Invalid parameter: the model does not support one or
more of the provided input parameters`, independent of `tool_choice`. The
shim does not address this. Use `@ai-sdk/openai-compatible`.

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
