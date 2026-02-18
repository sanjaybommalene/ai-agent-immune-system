"""Tests for the gateway Flask app (management API endpoints)."""
import json
from unittest.mock import patch, MagicMock

import pytest


@pytest.fixture
def gateway_app():
    """Create a gateway Flask app with mocked upstream."""
    with patch.dict("os.environ", {"LLM_UPSTREAM_URL": "http://fake-llm:8080"}, clear=False):
        from gateway.app import create_app
        app = create_app()
        app.config["TESTING"] = True
        return app


@pytest.fixture
def client(gateway_app):
    return gateway_app.test_client()


class TestHealthEndpoint:
    def test_health_returns_ok(self, client):
        r = client.get("/health")
        assert r.status_code == 200
        data = r.get_json()
        assert data["status"] == "ok"
        assert "upstream" in data
        assert "agents_discovered" in data


class TestGatewayAgentsAPI:
    def test_agents_initially_empty(self, client):
        r = client.get("/api/gateway/agents")
        assert r.status_code == 200
        assert r.get_json() == []


class TestGatewayStatsAPI:
    def test_stats_returns_structure(self, client):
        r = client.get("/api/gateway/stats")
        assert r.status_code == 200
        data = r.get_json()
        assert "agents_discovered" in data
        assert "total_requests_proxied" in data
        assert "baselines_learned" in data
        assert "active_anomalies" in data


class TestGatewayPoliciesAPI:
    def test_policies_returns_list(self, client):
        r = client.get("/api/gateway/policies")
        assert r.status_code == 200
        assert isinstance(r.get_json(), list)


class TestAgentVitalsAPI:
    def test_vitals_for_unknown_agent(self, client):
        r = client.get("/api/gateway/agent/unknown/vitals")
        assert r.status_code == 200
        assert r.get_json() == []


class TestAgentBaselineAPI:
    def test_baseline_not_ready_for_unknown(self, client):
        r = client.get("/api/gateway/agent/unknown/baseline")
        assert r.status_code == 200
        data = r.get_json()
        assert data["ready"] is False


class TestProxyPassthrough:
    def test_non_streaming_forward(self, client):
        """Proxy forwards to upstream and returns the response."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.content = json.dumps({
            "model": "gpt-4o",
            "choices": [{"message": {"content": "Hi"}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        }).encode()
        mock_response.headers = {"Content-Type": "application/json"}

        with patch("gateway.proxy.LLMProxy.forward", return_value=(200, {"Content-Type": "application/json"}, mock_response.content)):
            r = client.post(
                "/v1/chat/completions",
                data=json.dumps({"model": "gpt-4o", "messages": [{"role": "user", "content": "Hi"}]}),
                content_type="application/json",
            )
            assert r.status_code == 200
            data = r.get_json()
            assert data["model"] == "gpt-4o"


class TestCacheControlHeader:
    def test_responses_have_no_store(self, client):
        with patch("gateway.proxy.LLMProxy.forward", return_value=(200, {"Content-Type": "application/json"}, b'{"ok":true}')):
            r = client.post(
                "/v1/chat/completions",
                data=json.dumps({"model": "gpt-4o", "messages": []}),
                content_type="application/json",
            )
            assert r.headers.get("Cache-Control") == "no-store"
