"""
OTEL Span Processor — Consume OpenTelemetry traces emitted by AI agent
frameworks (LangChain, LlamaIndex, OpenLLMetry, etc.) and convert them
into immune-system vitals.

Many agent frameworks already instrument LLM calls with OTEL spans using
the ``gen_ai.*`` semantic conventions:

    gen_ai.system          = "openai" | "anthropic" | ...
    gen_ai.request.model   = "gpt-4o"
    gen_ai.usage.prompt_tokens       = 120
    gen_ai.usage.completion_tokens   = 45
    gen_ai.response.finish_reasons   = ["stop"]

This processor hooks into the OTEL SDK trace pipeline, intercepts these
spans as they complete, and feeds the extracted vitals into the immune
system's telemetry collector.

Usage::

    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider
    from gateway.otel_processor import ImmuneSpanProcessor

    provider = TracerProvider()
    provider.add_span_processor(ImmuneSpanProcessor(telemetry, baseline_learner))
    trace.set_tracer_provider(provider)
"""
import hashlib
import time
from typing import Callable, Dict, List, Optional, Sequence

from immune_system.logging_config import get_logger

logger = get_logger("otel_processor")

_GEN_AI_SYSTEM = "gen_ai.system"
_GEN_AI_MODEL = "gen_ai.request.model"
_GEN_AI_PROMPT_TOKENS = "gen_ai.usage.prompt_tokens"
_GEN_AI_COMPLETION_TOKENS = "gen_ai.usage.completion_tokens"
_GEN_AI_FINISH_REASONS = "gen_ai.response.finish_reasons"

_LLM_SPAN_NAMES = frozenset({
    "chat", "completion", "ChatCompletion", "chat.completions.create",
    "openai.chat", "anthropic.messages", "llm",
})


def _is_llm_span(span) -> bool:
    """Heuristic: is this span an LLM call we should capture?"""
    attrs = span.attributes or {}
    if attrs.get(_GEN_AI_SYSTEM) or attrs.get(_GEN_AI_MODEL):
        return True
    name = (span.name or "").lower()
    return any(p in name for p in ("chat", "completion", "llm", "openai", "anthropic", "bedrock"))


def _span_to_vitals(span) -> Optional[Dict]:
    """Convert an OTEL span to an AgentVitals-compatible dict."""
    attrs = span.attributes or {}
    model = str(attrs.get(_GEN_AI_MODEL, attrs.get("llm.request.model", "unknown")))
    input_tokens = int(attrs.get(_GEN_AI_PROMPT_TOKENS, attrs.get("llm.usage.prompt_tokens", 0)))
    output_tokens = int(attrs.get(_GEN_AI_COMPLETION_TOKENS, attrs.get("llm.usage.completion_tokens", 0)))

    start_ns = span.start_time or 0
    end_ns = span.end_time or 0
    latency_ms = max(0, (end_ns - start_ns) // 1_000_000) if start_ns and end_ns else 0

    finish_reasons = attrs.get(_GEN_AI_FINISH_REASONS, [])
    if isinstance(finish_reasons, str):
        finish_reasons = [finish_reasons]
    tool_calls = sum(1 for r in finish_reasons if "tool" in str(r).lower() or "function" in str(r).lower())

    status_ok = True
    error_type = ""
    if hasattr(span, "status") and span.status:
        from opentelemetry.trace import StatusCode
        if span.status.status_code == StatusCode.ERROR:
            status_ok = False
            error_type = span.status.description or "otel_error"

    service_name = ""
    if hasattr(span, "resource") and span.resource:
        service_name = str(span.resource.attributes.get("service.name", ""))

    agent_id = str(attrs.get("agent.id", attrs.get("user.id", service_name or "otel-agent")))
    agent_type = str(attrs.get("agent.type", "otel"))

    system_prompt = str(attrs.get("gen_ai.prompt.0.content", attrs.get("llm.prompts.0.content", "")))
    prompt_hash = hashlib.sha256(system_prompt.encode()).hexdigest()[:16] if system_prompt else ""

    return {
        "agent_id": agent_id,
        "agent_type": agent_type,
        "latency_ms": int(latency_ms),
        "token_count": input_tokens + output_tokens,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "tool_calls": tool_calls,
        "retries": 0,
        "success": status_ok,
        "cost": 0.0,
        "model": model,
        "error_type": error_type,
        "prompt_hash": prompt_hash,
        "timestamp": time.time(),
    }


class ImmuneSpanProcessor:
    """OTEL SpanProcessor that feeds LLM spans into the immune system.

    Implements the ``opentelemetry.sdk.trace.SpanProcessor`` protocol so it
    can be added to any ``TracerProvider``.
    """

    def __init__(self, on_vitals: Optional[Callable[[Dict], None]] = None):
        self.on_vitals = on_vitals
        self._count = 0

    def on_start(self, span, parent_context=None):
        pass

    def on_end(self, span):
        if not _is_llm_span(span):
            return
        vitals = _span_to_vitals(span)
        if vitals and self.on_vitals:
            self.on_vitals(vitals)
            self._count += 1
            if self._count % 100 == 0:
                logger.info("Processed %d LLM spans via OTEL", self._count)

    def shutdown(self):
        logger.info("OTEL ImmuneSpanProcessor shutdown (processed %d spans)", self._count)

    def force_flush(self, timeout_millis=None):
        pass
