"""Tests for the OTEL span processor that converts gen_ai spans into vitals."""
from unittest.mock import MagicMock

import pytest

from gateway.otel_processor import ImmuneSpanProcessor, _is_llm_span, _span_to_vitals


def _make_span(name="chat.completions.create", attributes=None, start_ns=0, end_ns=100_000_000):
    """Create a mock OTEL span."""
    span = MagicMock()
    span.name = name
    span.attributes = attributes or {}
    span.start_time = start_ns
    span.end_time = end_ns
    span.status = None
    span.resource = None
    return span


class TestIsLLMSpan:
    def test_gen_ai_system_attribute(self):
        span = _make_span(name="irrelevant", attributes={"gen_ai.system": "openai"})
        assert _is_llm_span(span) is True

    def test_gen_ai_model_attribute(self):
        span = _make_span(name="irrelevant", attributes={"gen_ai.request.model": "gpt-4o"})
        assert _is_llm_span(span) is True

    def test_chat_in_name(self):
        span = _make_span(name="openai.chat")
        assert _is_llm_span(span) is True

    def test_unrelated_span(self):
        span = _make_span(name="db.query", attributes={})
        assert _is_llm_span(span) is False


class TestSpanToVitals:
    def test_basic_extraction(self):
        span = _make_span(
            attributes={
                "gen_ai.request.model": "gpt-4o",
                "gen_ai.usage.prompt_tokens": 100,
                "gen_ai.usage.completion_tokens": 50,
                "gen_ai.response.finish_reasons": ["stop"],
            },
            start_ns=1_000_000_000,
            end_ns=1_150_000_000,
        )
        vitals = _span_to_vitals(span)
        assert vitals is not None
        assert vitals["model"] == "gpt-4o"
        assert vitals["input_tokens"] == 100
        assert vitals["output_tokens"] == 50
        assert vitals["latency_ms"] == 150
        assert vitals["tool_calls"] == 0
        assert vitals["success"] is True

    def test_tool_call_detection(self):
        span = _make_span(
            attributes={
                "gen_ai.request.model": "gpt-4o",
                "gen_ai.response.finish_reasons": ["tool_calls"],
            },
        )
        vitals = _span_to_vitals(span)
        assert vitals["tool_calls"] == 1

    def test_error_status(self):
        from opentelemetry.trace import StatusCode, Status
        span = _make_span(attributes={"gen_ai.request.model": "gpt-4o"})
        span.status = Status(status_code=StatusCode.ERROR, description="rate_limit")
        vitals = _span_to_vitals(span)
        assert vitals["success"] is False
        assert vitals["error_type"] == "rate_limit"

    def test_agent_id_from_attribute(self):
        span = _make_span(attributes={"gen_ai.request.model": "gpt-4o", "agent.id": "my-agent"})
        vitals = _span_to_vitals(span)
        assert vitals["agent_id"] == "my-agent"

    def test_fallback_agent_id(self):
        span = _make_span(attributes={"gen_ai.request.model": "gpt-4o"})
        vitals = _span_to_vitals(span)
        assert vitals["agent_id"] == "otel-agent"


class TestImmuneSpanProcessor:
    def test_on_end_emits_vitals_for_llm_span(self):
        received = []
        processor = ImmuneSpanProcessor(on_vitals=received.append)
        span = _make_span(
            attributes={
                "gen_ai.system": "openai",
                "gen_ai.request.model": "gpt-4o",
                "gen_ai.usage.prompt_tokens": 10,
                "gen_ai.usage.completion_tokens": 5,
            },
        )
        processor.on_end(span)
        assert len(received) == 1
        assert received[0]["model"] == "gpt-4o"

    def test_on_end_ignores_non_llm_span(self):
        received = []
        processor = ImmuneSpanProcessor(on_vitals=received.append)
        span = _make_span(name="db.query", attributes={})
        processor.on_end(span)
        assert len(received) == 0

    def test_on_start_is_noop(self):
        processor = ImmuneSpanProcessor()
        processor.on_start(None)  # Should not raise
