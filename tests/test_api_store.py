"""Tests for ApiStore: contract (paths, headers, payloads) and run_id isolation via headers."""
import time
from unittest.mock import patch, MagicMock

import pytest

from immune_system.api_store import ApiStore


@pytest.fixture
def mock_requests():
    with patch("immune_system.api_store.requests") as m:
        m.get = MagicMock()
        m.post = MagicMock()
        yield m


class TestApiStoreHeaders:
    def test_run_id_in_headers(self, mock_requests):
        mock_requests.get.return_value.status_code = 200
        mock_requests.get.return_value.content = b"[]"
        mock_requests.get.return_value.json.return_value = []
        store = ApiStore(base_url="https://api.example.com", run_id="run-abc")
        store.get_recent_agent_vitals("a1", window_seconds=10)
        call_kw = mock_requests.get.call_args.kwargs
        assert call_kw["headers"].get("X-Run-Id") == "run-abc"

    def test_api_key_in_headers(self, mock_requests):
        mock_requests.post.return_value.status_code = 204
        store = ApiStore(base_url="https://api.example.com", api_key="secret-123")
        store.write_agent_vitals({
            "agent_id": "a1", "agent_type": "test", "latency_ms": 100, "token_count": 200,
            "tool_calls": 2, "retries": 0, "success": True, "timestamp": time.time(),
        })
        call_kw = mock_requests.post.call_args.kwargs
        assert call_kw["headers"].get("X-API-Key") == "secret-123"

    def test_bearer_api_key_uses_authorization_header(self, mock_requests):
        mock_requests.get.return_value.status_code = 200
        mock_requests.get.return_value.content = b"{}"
        mock_requests.get.return_value.json.return_value = {}
        store = ApiStore(base_url="https://api.example.com", api_key="Bearer token-xyz")
        store.get_baseline_profile("a1")
        call_kw = mock_requests.get.call_args.kwargs
        assert call_kw["headers"].get("Authorization") == "Bearer token-xyz"


class TestApiStoreVitalsContract:
    def test_write_agent_vitals_posts_to_vitals_path(self, mock_requests):
        mock_requests.post.return_value.status_code = 204
        store = ApiStore(base_url="https://api.example.com")
        store.write_agent_vitals({
            "agent_id": "agent-1",
            "agent_type": "external",
            "latency_ms": 150,
            "token_count": 300,
            "input_tokens": 100,
            "output_tokens": 200,
            "tool_calls": 3,
            "retries": 0,
            "success": True,
            "cost": 0.005,
            "model": "gpt-4o",
            "error_type": "",
            "prompt_hash": "abc",
            "timestamp": time.time(),
        })
        mock_requests.post.assert_called_once()
        url = mock_requests.post.call_args.args[0]
        assert url == "https://api.example.com/api/v1/vitals"
        payload = mock_requests.post.call_args.kwargs["json"]
        assert payload["agent_id"] == "agent-1"
        assert payload["latency_ms"] == 150
        assert payload["token_count"] == 300
        assert payload["input_tokens"] == 100
        assert payload["output_tokens"] == 200
        assert "timestamp" in payload

    def test_get_recent_agent_vitals_gets_with_params(self, mock_requests):
        mock_requests.get.return_value.status_code = 200
        mock_requests.get.return_value.content = b"[]"
        mock_requests.get.return_value.json.return_value = []
        store = ApiStore(base_url="https://api.example.com")
        store.get_recent_agent_vitals("a1", window_seconds=10)
        call_kw = mock_requests.get.call_args.kwargs
        assert call_kw["params"]["agent_id"] == "a1"
        assert call_kw["params"]["window_seconds"] == 10
        assert "/api/v1/vitals/recent" in mock_requests.get.call_args.args[0]


class TestApiStoreErrorPropagation:
    def test_get_raises_on_http_error(self, mock_requests):
        mock_requests.get.return_value.raise_for_status.side_effect = Exception("404")
        mock_requests.get.return_value.status_code = 404
        store = ApiStore(base_url="https://api.example.com")
        with pytest.raises(Exception):
            store.get_recent_agent_vitals("a1", window_seconds=10)

    def test_post_raises_on_http_error(self, mock_requests):
        mock_requests.post.return_value.raise_for_status.side_effect = Exception("500")
        store = ApiStore(base_url="https://api.example.com")
        with pytest.raises(Exception):
            store.write_agent_vitals({
                "agent_id": "a1", "agent_type": "t", "latency_ms": 0, "token_count": 0,
                "tool_calls": 0, "retries": 0, "success": True, "timestamp": time.time(),
            })
