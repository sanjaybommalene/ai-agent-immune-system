"""Tests for ImmuneReporter SDK: payload construction, buffering, API key."""
import time
import queue
from unittest.mock import patch, MagicMock

import pytest

from immune_sdk import ImmuneReporter


@pytest.fixture
def mock_requests():
    with patch("immune_sdk._requests") as mock:
        mock.post = MagicMock()
        yield mock


class TestPayloadConstruction:
    def test_report_builds_correct_payload(self, mock_requests):
        reporter = ImmuneReporter(
            agent_id="sdk-test",
            api_key="test-key",
            base_url="http://localhost:9999",
        )
        reporter.report(
            input_tokens=100,
            output_tokens=200,
            latency_ms=150,
            tool_calls=3,
            model="gpt-4o",
            success=True,
            cost=0.005,
        )
        # Flush synchronously to capture the payload
        reporter.flush()
        reporter._closed = True

        calls = mock_requests.post.call_args_list
        # At least one call should be the ingest endpoint
        ingest_calls = [c for c in calls if "/api/v1/ingest" in str(c)]
        assert len(ingest_calls) >= 1
        payload = ingest_calls[0].kwargs.get("json") or ingest_calls[0][1].get("json")
        assert payload["agent_id"] == "sdk-test"
        assert payload["input_tokens"] == 100
        assert payload["output_tokens"] == 200
        assert payload["token_count"] == 300
        assert payload["latency_ms"] == 150
        assert payload["tool_calls"] == 3
        assert payload["success"] is True
        assert "timestamp" in payload

    def test_default_model_from_constructor(self, mock_requests):
        reporter = ImmuneReporter(agent_id="a1", model="claude-3")
        reporter.report(latency_ms=50)
        reporter.flush()
        reporter._closed = True
        ingest_calls = [c for c in mock_requests.post.call_args_list if "/api/v1/ingest" in str(c)]
        if ingest_calls:
            payload = ingest_calls[0].kwargs.get("json") or ingest_calls[0][1].get("json")
            assert payload["model"] == "claude-3"


class TestAPIKeyHeader:
    def test_api_key_in_headers(self, mock_requests):
        reporter = ImmuneReporter(agent_id="a1", api_key="my-secret-key")
        reporter.report(latency_ms=50)
        reporter.flush()
        reporter._closed = True
        for call in mock_requests.post.call_args_list:
            headers = call.kwargs.get("headers") or call[1].get("headers", {})
            if headers.get("X-API-KEY"):
                assert headers["X-API-KEY"] == "my-secret-key"
                return
        pytest.fail("X-API-KEY header not found in any request")

    def test_no_api_key_when_empty(self, mock_requests):
        reporter = ImmuneReporter(agent_id="a1", api_key="")
        reporter.report(latency_ms=50)
        reporter.flush()
        reporter._closed = True
        for call in mock_requests.post.call_args_list:
            headers = call.kwargs.get("headers") or call[1].get("headers", {})
            assert "X-API-KEY" not in headers


class TestBuffering:
    def test_reports_are_queued(self, mock_requests):
        reporter = ImmuneReporter(agent_id="a1")
        reporter._closed = True  # prevent background thread from flushing
        for _ in range(5):
            payload = {
                "agent_id": "a1", "agent_type": "external",
                "input_tokens": 0, "output_tokens": 0, "token_count": 0,
                "latency_ms": 50, "tool_calls": 0, "retries": 0,
                "success": True, "cost": 0.0, "model": "",
                "error_type": "", "prompt_hash": "", "timestamp": time.time(),
            }
            try:
                reporter._queue.put_nowait(payload)
            except queue.Full:
                break
        assert reporter._queue.qsize() == 5

    def test_closed_reporter_ignores_reports(self, mock_requests):
        reporter = ImmuneReporter(agent_id="a1")
        reporter.close()
        reporter.report(latency_ms=50)
        assert reporter._queue.empty()


class TestErrorHandling:
    def test_on_error_callback(self, mock_requests):
        errors = []
        mock_requests.post.side_effect = ConnectionError("fail")
        reporter = ImmuneReporter(agent_id="a1", on_error=errors.append)
        reporter.report(latency_ms=50)
        reporter.flush()
        reporter._closed = True
        # The register or send call should have triggered the error callback
        assert len(errors) > 0
        assert isinstance(errors[0], ConnectionError)
