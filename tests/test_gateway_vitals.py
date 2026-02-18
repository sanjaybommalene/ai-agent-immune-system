"""Tests for gateway vitals extraction from OpenAI-format request/response pairs."""
import pytest

from gateway.vitals_extractor import (
    extract_vitals,
    extract_vitals_from_stream_chunks,
    _count_tool_calls,
    _estimate_cost,
    _extract_system_prompt,
    _match_cost,
)


class TestExtractSystemPrompt:
    def test_single_system_message(self):
        messages = [{"role": "system", "content": "You are helpful."}]
        assert _extract_system_prompt(messages) == "You are helpful."

    def test_developer_role(self):
        messages = [{"role": "developer", "content": "Be concise."}]
        assert _extract_system_prompt(messages) == "Be concise."

    def test_multiple_system_messages(self):
        messages = [
            {"role": "system", "content": "Line 1"},
            {"role": "user", "content": "Hello"},
            {"role": "system", "content": "Line 2"},
        ]
        assert _extract_system_prompt(messages) == "Line 1\nLine 2"

    def test_no_system_message(self):
        messages = [{"role": "user", "content": "Hello"}]
        assert _extract_system_prompt(messages) == ""

    def test_content_as_list(self):
        messages = [{"role": "system", "content": [{"type": "text", "text": "From list"}]}]
        assert _extract_system_prompt(messages) == "From list"

    def test_empty_messages(self):
        assert _extract_system_prompt([]) == ""


class TestCountToolCalls:
    def test_no_tool_calls(self):
        body = {"choices": [{"message": {"role": "assistant", "content": "Hi"}}]}
        assert _count_tool_calls(body) == 0

    def test_tool_calls_list(self):
        body = {
            "choices": [{
                "message": {
                    "role": "assistant",
                    "tool_calls": [
                        {"id": "tc1", "type": "function", "function": {"name": "search"}},
                        {"id": "tc2", "type": "function", "function": {"name": "read"}},
                    ],
                },
            }],
        }
        assert _count_tool_calls(body) == 2

    def test_legacy_function_call(self):
        body = {
            "choices": [{
                "message": {
                    "role": "assistant",
                    "function_call": {"name": "search", "arguments": "{}"},
                },
            }],
        }
        assert _count_tool_calls(body) == 1

    def test_empty_choices(self):
        assert _count_tool_calls({"choices": []}) == 0
        assert _count_tool_calls({}) == 0


class TestCostEstimation:
    def test_known_model(self):
        rates = _match_cost("gpt-4o")
        cost = _estimate_cost("gpt-4o", input_tokens=1000, output_tokens=500)
        assert cost > 0
        expected = 1000 * rates["input"] / 1000 + 500 * rates["output"] / 1000
        assert cost == pytest.approx(expected, abs=1e-5)

    def test_unknown_model_uses_default(self):
        cost = _estimate_cost("some-unknown-model", input_tokens=1000, output_tokens=500)
        assert cost > 0

    def test_zero_tokens(self):
        assert _estimate_cost("gpt-4o", 0, 0) == 0.0

    def test_model_substring_matching(self):
        rates_a = _match_cost("ft:gpt-4o-mini:my-org:custom")
        rates_b = _match_cost("gpt-4o-mini")
        assert rates_a == rates_b


class TestExtractVitals:
    def test_full_extraction(self):
        req = {
            "model": "gpt-4o",
            "messages": [
                {"role": "system", "content": "Be helpful"},
                {"role": "user", "content": "Hello"},
            ],
        }
        resp = {
            "model": "gpt-4o-2024-08-06",
            "choices": [{"message": {"role": "assistant", "content": "Hi!"}}],
            "usage": {"prompt_tokens": 20, "completion_tokens": 5, "total_tokens": 25},
        }
        vitals = extract_vitals(
            request_body=req,
            response_body=resp,
            latency_ms=150,
            agent_id="agent-1",
        )
        assert vitals["agent_id"] == "agent-1"
        assert vitals["model"] == "gpt-4o-2024-08-06"
        assert vitals["input_tokens"] == 20
        assert vitals["output_tokens"] == 5
        assert vitals["token_count"] == 25
        assert vitals["latency_ms"] == 150
        assert vitals["success"] is True
        assert vitals["tool_calls"] == 0
        assert vitals["prompt_hash"] != ""
        assert vitals["cost"] > 0
        assert "timestamp" in vitals

    def test_error_response(self):
        vitals = extract_vitals(
            request_body={"model": "gpt-4"},
            response_body=None,
            latency_ms=50,
            agent_id="agent-1",
            success=False,
            error_type="timeout",
        )
        assert vitals["success"] is False
        assert vitals["error_type"] == "timeout"
        assert vitals["input_tokens"] == 0
        assert vitals["output_tokens"] == 0

    def test_tool_calls_counted(self):
        resp = {
            "model": "gpt-4o",
            "choices": [{
                "message": {
                    "tool_calls": [{"id": "t1", "function": {"name": "f"}}],
                },
            }],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        }
        vitals = extract_vitals(
            request_body={"model": "gpt-4o", "messages": []},
            response_body=resp,
            latency_ms=100,
            agent_id="a1",
        )
        assert vitals["tool_calls"] == 1

    def test_model_from_request_when_response_missing(self):
        vitals = extract_vitals(
            request_body={"model": "claude-3-sonnet", "messages": []},
            response_body=None,
            latency_ms=100,
            agent_id="a1",
        )
        assert vitals["model"] == "claude-3-sonnet"


class TestStreamChunkExtraction:
    def test_with_usage_in_final_chunk(self):
        req = {"model": "gpt-4o", "messages": [{"role": "user", "content": "Hi"}]}
        chunks = [
            {"model": "gpt-4o", "choices": [{"delta": {"content": "Hello"}}]},
            {"model": "gpt-4o", "choices": [{"delta": {"content": " there"}}]},
            {"model": "gpt-4o", "choices": [{"delta": {}}], "usage": {"prompt_tokens": 10, "completion_tokens": 4, "total_tokens": 14}},
        ]
        vitals = extract_vitals_from_stream_chunks(
            request_body=req,
            chunks=chunks,
            latency_ms=200,
            agent_id="a1",
        )
        assert vitals["input_tokens"] == 10
        assert vitals["output_tokens"] == 4
        assert vitals["token_count"] == 14
        assert vitals["latency_ms"] == 200

    def test_without_usage_estimates_from_content(self):
        req = {"model": "gpt-4o", "messages": []}
        chunks = [
            {"choices": [{"delta": {"content": "Hello world, this is a test response"}}]},
        ]
        vitals = extract_vitals_from_stream_chunks(
            request_body=req,
            chunks=chunks,
            latency_ms=100,
            agent_id="a1",
        )
        assert vitals["output_tokens"] >= 1
        assert vitals["input_tokens"] == 0
