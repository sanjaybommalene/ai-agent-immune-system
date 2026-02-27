"""Tests for the agent auto-discovery service."""
import pytest

from gateway.discovery import DiscoveryService


@pytest.fixture
def svc():
    return DiscoveryService()


class TestObserve:
    def test_new_agent_creates_record(self, svc):
        rec = svc.observe("agent-1", agent_type="LangChain", model="gpt-4o", source_ip="10.0.0.1")
        assert rec.agent_id == "agent-1"
        assert rec.agent_type == "LangChain"
        assert rec.request_count == 1
        assert "gpt-4o" in rec.models_used
        assert "10.0.0.1" in rec.source_ips
        assert rec.first_seen > 0
        assert rec.first_seen == rec.last_seen

    def test_repeat_observation_increments_count(self, svc):
        svc.observe("agent-1")
        svc.observe("agent-1")
        rec = svc.observe("agent-1")
        assert rec.request_count == 3

    def test_models_accumulated(self, svc):
        svc.observe("agent-1", model="gpt-4o")
        svc.observe("agent-1", model="gpt-3.5-turbo")
        rec = svc.get_agent("agent-1")
        assert rec.models_used == {"gpt-4o", "gpt-3.5-turbo"}

    def test_source_ips_accumulated(self, svc):
        svc.observe("agent-1", source_ip="10.0.0.1")
        svc.observe("agent-1", source_ip="10.0.0.2")
        rec = svc.get_agent("agent-1")
        assert rec.source_ips == {"10.0.0.1", "10.0.0.2"}

    def test_last_seen_updates(self, svc):
        svc.observe("agent-1")
        first = svc.get_agent("agent-1").last_seen
        import time
        time.sleep(0.01)
        svc.observe("agent-1")
        assert svc.get_agent("agent-1").last_seen >= first

    def test_agent_type_upgrades_from_external(self, svc):
        svc.observe("agent-1", agent_type="external")
        svc.observe("agent-1", agent_type="LangChain")
        assert svc.get_agent("agent-1").agent_type == "LangChain"

    def test_agent_type_does_not_downgrade(self, svc):
        svc.observe("agent-1", agent_type="LangChain")
        svc.observe("agent-1", agent_type="external")
        assert svc.get_agent("agent-1").agent_type == "LangChain"


class TestNewAgentCallback:
    def test_callback_fires_for_new_agent(self):
        seen = []
        svc = DiscoveryService(on_new_agent=seen.append)
        svc.observe("agent-1")
        assert len(seen) == 1
        assert seen[0].agent_id == "agent-1"

    def test_callback_does_not_fire_for_repeat(self):
        seen = []
        svc = DiscoveryService(on_new_agent=seen.append)
        svc.observe("agent-1")
        svc.observe("agent-1")
        assert len(seen) == 1


class TestListAndCount:
    def test_list_agents(self, svc):
        svc.observe("a1")
        svc.observe("a2")
        result = svc.list_agents()
        ids = [a["agent_id"] for a in result]
        assert "a1" in ids
        assert "a2" in ids

    def test_count(self, svc):
        assert svc.count() == 0
        svc.observe("a1")
        svc.observe("a2")
        assert svc.count() == 2

    def test_get_missing_agent(self, svc):
        assert svc.get_agent("nonexistent") is None
