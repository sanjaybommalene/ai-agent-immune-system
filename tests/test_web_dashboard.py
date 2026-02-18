"""Tests for Web Dashboard: ingest, approve/heal-explicitly, read-only APIs, API key on ingest."""
import time

import pytest

from immune_system.agents import BaseAgent
from immune_system.orchestrator import ImmuneSystemOrchestrator
from immune_system.web_dashboard import WebDashboard


@pytest.fixture
def orchestrator_with_one_agent():
    agent = BaseAgent("test-agent", "test")
    orch = ImmuneSystemOrchestrator([agent])
    return orch


@pytest.fixture
def dashboard(orchestrator_with_one_agent):
    dash = WebDashboard(orchestrator_with_one_agent, port=0, api_key="")
    dash.app.config["TESTING"] = True
    return dash


@pytest.fixture
def client(dashboard):
    return dashboard.app.test_client()


class TestReadOnlyEndpoints:
    def test_get_status(self, client):
        r = client.get("/api/status")
        assert r.status_code == 200
        data = r.get_json()
        assert "running" in data
        assert "baselines_learned" in data

    def test_get_agents(self, client):
        r = client.get("/api/agents")
        assert r.status_code == 200
        data = r.get_json()
        assert isinstance(data, list)
        assert len(data) >= 1
        assert any(a["id"] == "test-agent" for a in data)

    def test_get_stats(self, client):
        r = client.get("/api/stats")
        assert r.status_code == 200
        data = r.get_json()
        assert "total_agents" in data
        assert "total_executions" in data
        assert "total_infections" in data

    def test_get_pending_approvals(self, client):
        r = client.get("/api/pending-approvals")
        assert r.status_code == 200
        assert r.get_json() == []

    def test_get_rejected_approvals(self, client):
        r = client.get("/api/rejected-approvals")
        assert r.status_code == 200
        assert r.get_json() == []


class TestIngest:
    def test_ingest_valid_vitals(self, client, orchestrator_with_one_agent):
        r = client.post(
            "/api/v1/ingest",
            json={
                "agent_id": "external-1",
                "agent_type": "external",
                "latency_ms": 100,
                "input_tokens": 50,
                "output_tokens": 60,
                "tool_calls": 2,
                "retries": 0,
                "success": True,
                "cost": 0.002,
                "model": "gpt-4o",
            },
            content_type="application/json",
        )
        assert r.status_code == 200
        assert r.get_json() == {"ok": True}
        # External agent is auto-registered
        assert "external-1" in orchestrator_with_one_agent.agents
        # Telemetry should have been recorded (in-memory or store)
        assert orchestrator_with_one_agent.telemetry.get_count("external-1") >= 1

    def test_ingest_missing_agent_id_returns_400(self, client):
        r = client.post(
            "/api/v1/ingest",
            json={"latency_ms": 100},
            content_type="application/json",
        )
        assert r.status_code == 400
        data = r.get_json()
        assert data.get("ok") is False
        assert "agent_id" in (data.get("error") or "").lower()

    def test_ingest_invalid_numeric_returns_400(self, client):
        r = client.post(
            "/api/v1/ingest",
            json={"agent_id": "a1", "latency_ms": "not-a-number"},
            content_type="application/json",
        )
        assert r.status_code == 400

    def test_ingest_requires_api_key_when_configured(self, orchestrator_with_one_agent):
        dash = WebDashboard(orchestrator_with_one_agent, port=0, api_key="required-key")
        dash.app.config["TESTING"] = True
        c = dash.app.test_client()
        r = c.post(
            "/api/v1/ingest",
            json={"agent_id": "a1", "latency_ms": 100},
            content_type="application/json",
        )
        assert r.status_code == 401
        r2 = c.post(
            "/api/v1/ingest",
            json={"agent_id": "a1", "latency_ms": 100},
            headers={"X-API-KEY": "required-key"},
            content_type="application/json",
        )
        assert r2.status_code == 200


class TestApproveHealing:
    def test_approve_healing_missing_agent_id_returns_400(self, client):
        r = client.post(
            "/api/approve-healing",
            json={"approved": True},
            content_type="application/json",
        )
        assert r.status_code == 400
        assert r.get_json().get("error", "").lower().find("agent_id") >= 0

    def test_approve_healing_no_pending_returns_ok_approved_false(self, client):
        r = client.post(
            "/api/approve-healing",
            json={"agent_id": "test-agent", "approved": True},
            content_type="application/json",
        )
        assert r.status_code == 200
        data = r.get_json()
        assert data.get("ok") is True
        assert data.get("approved") is False  # no pending for this agent


class TestHealExplicitly:
    def test_heal_explicitly_missing_agent_id_returns_400(self, client):
        r = client.post(
            "/api/heal-explicitly",
            json={},
            content_type="application/json",
        )
        assert r.status_code == 400
        assert "agent_id" in (r.get_json().get("error") or "").lower()

    def test_heal_explicitly_not_rejected_returns_ok_false(self, client):
        r = client.post(
            "/api/heal-explicitly",
            json={"agent_id": "test-agent"},
            content_type="application/json",
        )
        assert r.status_code == 200
        assert r.get_json().get("ok") is False  # agent was not in rejected list
