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

    def test_health_includes_providers(self, client):
        r = client.get("/health")
        data = r.get_json()
        assert "providers" in data
        assert "default" in data["providers"]


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


# ── Provider management API ──────────────────────────────────────────────


class TestProviderManagementAPI:
    def test_list_providers_default_only(self, client):
        r = client.get("/api/gateway/providers")
        assert r.status_code == 200
        data = r.get_json()
        assert "default" in data

    def test_register_provider(self, client):
        r = client.post(
            "/api/gateway/providers",
            data=json.dumps({"name": "azure", "url": "https://myresource.openai.azure.com"}),
            content_type="application/json",
        )
        assert r.status_code == 201
        data = r.get_json()
        assert data["registered"] is True
        assert data["name"] == "azure"

        r2 = client.get("/api/gateway/providers")
        assert "azure" in r2.get_json()

    def test_register_provider_missing_fields(self, client):
        r = client.post(
            "/api/gateway/providers",
            data=json.dumps({"name": ""}),
            content_type="application/json",
        )
        assert r.status_code == 400

    def test_register_provider_invalid_url(self, client):
        r = client.post(
            "/api/gateway/providers",
            data=json.dumps({"name": "bad", "url": "ftp://example.com"}),
            content_type="application/json",
        )
        assert r.status_code == 400

    def test_unregister_provider(self, client):
        client.post(
            "/api/gateway/providers",
            data=json.dumps({"name": "temp", "url": "https://temp.example.com"}),
            content_type="application/json",
        )
        r = client.delete("/api/gateway/providers/temp")
        assert r.status_code == 200
        data = r.get_json()
        assert data["unregistered"] is True

    def test_cannot_unregister_default(self, client):
        r = client.delete("/api/gateway/providers/default")
        assert r.status_code == 400

    def test_unregister_nonexistent(self, client):
        r = client.delete("/api/gateway/providers/ghost")
        assert r.status_code == 400


# ── Route management API ─────────────────────────────────────────────────


class TestRouteManagementAPI:
    def test_list_routes_initially_empty(self, client):
        r = client.get("/api/gateway/routes")
        assert r.status_code == 200
        assert r.get_json() == {}

    def test_set_route(self, client):
        client.post(
            "/api/gateway/providers",
            data=json.dumps({"name": "azure", "url": "https://azure.openai.com"}),
            content_type="application/json",
        )
        r = client.post(
            "/api/gateway/routes",
            data=json.dumps({"agent_id": "agent-1", "provider": "azure"}),
            content_type="application/json",
        )
        assert r.status_code == 201
        data = r.get_json()
        assert data["routed"] is True

        r2 = client.get("/api/gateway/routes")
        assert r2.get_json() == {"agent-1": "azure"}

    def test_set_route_unknown_provider(self, client):
        r = client.post(
            "/api/gateway/routes",
            data=json.dumps({"agent_id": "agent-1", "provider": "nonexistent"}),
            content_type="application/json",
        )
        assert r.status_code == 400

    def test_set_route_missing_fields(self, client):
        r = client.post(
            "/api/gateway/routes",
            data=json.dumps({"agent_id": ""}),
            content_type="application/json",
        )
        assert r.status_code == 400

    def test_remove_route(self, client):
        client.post(
            "/api/gateway/providers",
            data=json.dumps({"name": "azure", "url": "https://azure.openai.com"}),
            content_type="application/json",
        )
        client.post(
            "/api/gateway/routes",
            data=json.dumps({"agent_id": "agent-1", "provider": "azure"}),
            content_type="application/json",
        )
        r = client.delete("/api/gateway/routes/agent-1")
        assert r.status_code == 200
        assert r.get_json()["removed"] is True

    def test_remove_nonexistent_route(self, client):
        r = client.delete("/api/gateway/routes/ghost")
        assert r.status_code == 404


# ── Header stripping ─────────────────────────────────────────────────────


class TestXLLMProviderHeaderStripping:
    def test_header_stripped_before_upstream(self):
        from gateway.proxy import LLMProxy
        incoming = {
            "Authorization": "Bearer sk-test",
            "X-LLM-Provider": "azure",
            "Content-Type": "application/json",
        }
        forwarded = LLMProxy._forward_headers(incoming)
        assert "X-LLM-Provider" not in forwarded
        assert "Authorization" in forwarded
        assert "Content-Type" in forwarded

    def test_header_stripped_case_insensitive(self):
        from gateway.proxy import LLMProxy
        incoming = {"x-llm-provider": "azure", "Accept": "*/*"}
        forwarded = LLMProxy._forward_headers(incoming)
        assert "x-llm-provider" not in forwarded
        assert "Accept" in forwarded
