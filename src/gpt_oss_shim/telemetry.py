"""OpenTelemetry tracing for forwarded chat requests.

Tracing is optional: without a tracer every call here is a no-op, and the
shim runs without OpenTelemetry installed (the ``otel`` extra adds it).

Spans follow the GenAI semantic conventions (``gen_ai.*``) and add what is
specific to this shim (``shim.outcome``, ``shim.polyfill.mode``). The
``gen_ai.*`` attributes describe the answer as the model produced it, so a
rescued answer keeps ``finish_reason: "stop"``; ``shim.outcome`` says what the
client received. The upstream host is deliberately not recorded: it holds the
Azure resource name, and traces usually leave the machine.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from .polyfill import Outcome, PolyfillMode

logger = logging.getLogger("gpt_oss_shim")

try:  # OpenTelemetry is an optional dependency.
    from opentelemetry.trace import SpanKind, Status, StatusCode
except ImportError:  # pragma: no cover - exercised only without the otel extra
    SpanKind = Status = StatusCode = None  # type: ignore[assignment,misc]

PROVIDER_NAME = "azure.ai.openai"


def response_summary(content: bytes, *, is_sse: bool) -> dict[str, Any]:
    """Pull id, model, usage and finish reasons out of an upstream answer."""
    try:
        events = _stream_events(content) if is_sse else [json.loads(content)]
    except ValueError:
        return {}

    summary: dict[str, Any] = {}
    finish_reasons: list[str] = []
    for event in events:
        if not isinstance(event, dict):
            continue
        summary.setdefault("response_id", event.get("id"))
        if event.get("model"):
            summary.setdefault("response_model", event["model"])
        usage = event.get("usage") or {}
        if usage:
            summary["input_tokens"] = usage.get("prompt_tokens")
            summary["output_tokens"] = usage.get("completion_tokens")
        for choice in event.get("choices") or []:
            if choice.get("finish_reason"):
                finish_reasons.append(choice["finish_reason"])
    if finish_reasons:
        summary["finish_reasons"] = finish_reasons
    return {key: value for key, value in summary.items() if value is not None}


def _stream_events(content: bytes) -> list[Any]:
    events = []
    for line in content.decode("utf-8", errors="replace").splitlines():
        if not line.startswith("data:"):
            continue
        data = line[len("data:") :].strip()
        if data and data != "[DONE]":
            events.append(json.loads(data))
    return events


class ChatSpan:
    """One span for one forwarded chat request; a no-op when tracing is off."""

    def __init__(self, span: Any | None) -> None:
        self._span = span

    def set_observed(self, outcome: Outcome) -> None:
        """Record the outcome a repair would have had, in observe mode."""
        if self._span is not None:
            self._span.set_attribute("shim.observed_outcome", outcome.value)

    def finish(
        self,
        outcome: Outcome,
        *,
        status_code: int | None = None,
        summary: dict[str, Any] | None = None,
        error: BaseException | None = None,
    ) -> None:
        if self._span is None:
            return
        try:
            self._span.set_attribute("shim.outcome", outcome.value)
            if status_code is not None:
                self._span.set_attribute("http.response.status_code", status_code)
            for key, value in (summary or {}).items():
                self._span.set_attribute(_GEN_AI_KEYS[key], value)
            if error is not None:
                self._span.set_attribute("error.type", type(error).__name__)
                self._span.set_status(Status(StatusCode.ERROR, str(error) or None))
        finally:
            self._span.end()


_GEN_AI_KEYS = {
    "response_id": "gen_ai.response.id",
    "response_model": "gen_ai.response.model",
    "input_tokens": "gen_ai.usage.input_tokens",
    "output_tokens": "gen_ai.usage.output_tokens",
    "finish_reasons": "gen_ai.response.finish_reasons",
}

NO_SPAN = ChatSpan(None)


class Tracing:
    """Creates spans when a tracer is configured, and nothing otherwise."""

    def __init__(self, tracer: Any | None = None) -> None:
        self._tracer = tracer if SpanKind is not None else None

    @property
    def enabled(self) -> bool:
        return self._tracer is not None

    def start_chat(self, *, model: str | None, forced: bool, mode: PolyfillMode) -> ChatSpan:
        if self._tracer is None:
            return NO_SPAN
        span = self._tracer.start_span(
            f"chat {model}" if model else "chat",
            kind=SpanKind.CLIENT,
            attributes={
                "gen_ai.operation.name": "chat",
                "gen_ai.provider.name": PROVIDER_NAME,
                "gen_ai.request.model": model or "",
                "shim.tool_choice.forced": forced,
                "shim.polyfill.mode": mode.value,
            },
        )
        return ChatSpan(span)


def build_tracer(version: str) -> Any | None:
    """Build a tracer from the standard OTEL environment variables, or ``None``.

    Tracing is on when ``OTEL_EXPORTER_OTLP_ENDPOINT`` (or the traces-specific
    variable) is set, or when ``OTEL_TRACES_EXPORTER=console``.
    """
    import os

    exporter_name = os.environ.get("OTEL_TRACES_EXPORTER", "").strip().lower()
    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT") or os.environ.get(
        "OTEL_EXPORTER_OTLP_ENDPOINT"
    )
    if not exporter_name:
        exporter_name = "otlp" if endpoint else "none"
    if exporter_name == "none":
        return None

    try:
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter
    except ImportError:
        logger.warning(
            "tracing is configured but not installed; pip install 'gpt-oss-azure-opencode-shim[otel]'"
        )
        return None

    if exporter_name == "console":
        exporter: Any = ConsoleSpanExporter()
    else:
        try:
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        except ImportError:
            logger.warning(
                "OTLP tracing is configured but not installed; pip install 'gpt-oss-azure-opencode-shim[otel]'"
            )
            return None
        exporter = OTLPSpanExporter()

    resource = Resource.create(
        {
            "service.name": os.environ.get("OTEL_SERVICE_NAME", "gpt-oss-azure-opencode-shim"),
            "service.version": version,
        }
    )
    provider = TracerProvider(resource=resource)
    provider.add_span_processor(BatchSpanProcessor(exporter))
    logger.info("tracing enabled, exporter=%s", exporter_name)

    import atexit

    atexit.register(provider.shutdown)
    return provider.get_tracer("gpt_oss_shim")
