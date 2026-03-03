"""Integration tests for ImmuneSystemOrchestrator.

Exercises the full pipeline: vitals -> baseline -> detection -> diagnosis ->
quarantine -> (approval) -> healing, using real components with in-memory
storage (no InfluxDB, no network).
"""
import asyncio
import time

import pytest

from immune_system.agents import BaseAgent
from immune_system.cache import CacheManager
from immune_system.orchestrator import ImmuneSystemOrchestrator, DEVIATION_REQUIRING_APPROVAL
from immune_system.telemetry import AgentVitals, TelemetryCollector
from immune_system.baseline import BaselineLearner
from immune_system.detection import Sentinel, AnomalyType, InfectionReport


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _build_vitals_dict(agent, **overrides):
    """Produce a vitals dict that looks like BaseAgent.execute() output."""
    base = {
        "timestamp": time.time(),
        "agent_id": agent.agent_id,
        "agent_type": agent.agent_type,
        "latency_ms": 120,
        "token_count": 300,
        "tool_calls": 2,
        "retries": 0,
        "success": True,
        "input_tokens": 150,
        "output_tokens": 150,
        "cost": 0.002,
        "model": agent.model_name,
        "error_type": "",
        "prompt_hash": "abc123",
    }
    base.update(overrides)
    return base


def _feed_normal_vitals(orchestrator, agent, n=20):
    """Manually feed *n* normal vitals through telemetry and baseline."""
    for _ in range(n):
        v = _build_vitals_dict(agent)
        orchestrator.telemetry.record(v)
        vitals_obj = AgentVitals(**{k: v[k] for k in AgentVitals.__dataclass_fields__})
        orchestrator.baseline_learner.update(agent.agent_id, vitals_obj)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestBaselineLearning:
    def test_baseline_learned_after_min_samples(self):
        agent = BaseAgent("a1", "test")
        orch = ImmuneSystemOrchestrator([agent])
        _feed_normal_vitals(orch, agent, n=20)
        assert orch.baseline_learner.has_baseline("a1")


class TestAnomalyDetection:
    def test_latency_spike_creates_infection(self):
        agent = BaseAgent("a1", "test")
        orch = ImmuneSystemOrchestrator([agent])
        _feed_normal_vitals(orch, agent, n=20)

        # Inject spike
        for _ in range(5):
            v = _build_vitals_dict(agent, latency_ms=1000)
            orch.telemetry.record(v)

        baseline = orch.baseline_learner.get_baseline("a1")
        recent = orch.telemetry.get_recent("a1", window_seconds=5)
        infection = orch.sentinel.detect_infection(recent, baseline)
        assert infection is not None
        assert AnomalyType.LATENCY_SPIKE in infection.anomalies


class TestQuarantineAndCache:
    def test_quarantine_persists_to_cache(self, tmp_path):
        cache = CacheManager(cache_dir=str(tmp_path))
        cache.load()
        agent = BaseAgent("a1", "test")
        orch = ImmuneSystemOrchestrator([agent], cache=cache)

        orch.quarantine.quarantine("a1")
        agent.quarantine()
        cache.add_quarantine("a1")
        cache.save()

        cache2 = CacheManager(cache_dir=str(tmp_path))
        cache2.load()
        assert "a1" in cache2.get_quarantine()

    def test_quarantine_restored_on_restart(self, tmp_path):
        cache = CacheManager(cache_dir=str(tmp_path))
        cache.load()
        cache.add_quarantine("a1")
        cache.save()

        cache2 = CacheManager(cache_dir=str(tmp_path))
        cache2.load()
        agent = BaseAgent("a1", "test")
        orch = ImmuneSystemOrchestrator([agent], cache=cache2)
        assert orch.quarantine.is_quarantined("a1")


class TestHITLApprovalFlow:
    def test_severe_infection_queued_for_approval(self):
        agent = BaseAgent("a1", "test")
        orch = ImmuneSystemOrchestrator([agent])

        infection = InfectionReport(
            agent_id="a1",
            max_deviation=DEVIATION_REQUIRING_APPROVAL + 1.0,
            anomalies=[AnomalyType.TOKEN_SPIKE],
            deviations={"tokens": DEVIATION_REQUIRING_APPROVAL + 1.0},
        )
        orch.quarantine.quarantine("a1")
        agent.quarantine()

        baseline = None
        _feed_normal_vitals(orch, agent, n=20)
        baseline = orch.baseline_learner.get_baseline("a1")
        diag = orch.diagnostician.diagnose(infection, baseline)

        # Simulate what sentinel_loop does for severe infections (in-memory mode)
        orch._pending_approvals["a1"] = {
            "infection": infection,
            "diagnosis": diag,
            "requested_at": time.time(),
        }

        pending = orch.get_pending_approvals()
        assert len(pending) == 1
        assert pending[0]["agent_id"] == "a1"

    def test_approve_healing(self):
        agent = BaseAgent("a1", "test")
        orch = ImmuneSystemOrchestrator([agent])
        _feed_normal_vitals(orch, agent, n=20)

        infection = InfectionReport(
            agent_id="a1",
            max_deviation=6.0,
            anomalies=[AnomalyType.TOKEN_SPIKE],
            deviations={"tokens": 6.0},
        )
        baseline = orch.baseline_learner.get_baseline("a1")
        diag = orch.diagnostician.diagnose(infection, baseline)
        orch._pending_approvals["a1"] = {
            "infection": infection,
            "diagnosis": diag,
            "requested_at": time.time(),
        }

        returned_infection, approved = orch.approve_healing("a1", True)
        assert approved
        assert returned_infection is infection
        assert orch.get_pending_approvals() == []

    def test_reject_healing(self):
        agent = BaseAgent("a1", "test")
        orch = ImmuneSystemOrchestrator([agent])
        _feed_normal_vitals(orch, agent, n=20)

        infection = InfectionReport(
            agent_id="a1",
            max_deviation=6.0,
            anomalies=[AnomalyType.TOKEN_SPIKE],
            deviations={"tokens": 6.0},
        )
        baseline = orch.baseline_learner.get_baseline("a1")
        diag = orch.diagnostician.diagnose(infection, baseline)
        orch._pending_approvals["a1"] = {
            "infection": infection,
            "diagnosis": diag,
            "requested_at": time.time(),
        }

        returned_infection, approved = orch.approve_healing("a1", False)
        assert not approved
        assert orch.get_rejected_approvals()

    def test_rejected_then_heal_now_removes_from_rejected_and_heals(self):
        """Real-world: user rejects healing then clicks 'Heal now'; agent is removed from rejected and healed."""
        agent = BaseAgent("a1", "test")
        orch = ImmuneSystemOrchestrator([agent])
        _feed_normal_vitals(orch, agent, n=20)

        infection = InfectionReport(
            agent_id="a1",
            max_deviation=6.0,
            anomalies=[AnomalyType.TOKEN_SPIKE],
            deviations={"tokens": 6.0},
        )
        baseline = orch.baseline_learner.get_baseline("a1")
        diag = orch.diagnostician.diagnose(infection, baseline)
        orch._pending_approvals["a1"] = {
            "infection": infection,
            "diagnosis": diag,
            "requested_at": time.time(),
        }
        orch.approve_healing("a1", False)
        assert len(orch.get_rejected_approvals()) == 1

        # Heal now: start_healing_explicitly returns infection and removes from rejected
        healed_infection = orch.start_healing_explicitly("a1")
        assert healed_infection is not None
        assert healed_infection.agent_id == "a1"
        assert len(orch.get_rejected_approvals()) == 0

        # Complete healing (as dashboard would schedule heal_agent)
        orch.quarantine.quarantine("a1")
        agent.quarantine()
        asyncio.run(orch.heal_agent("a1", healed_infection))
        assert not orch.quarantine.is_quarantined("a1")
        assert not agent.infected


class TestAutoHealFlow:
    @pytest.mark.asyncio
    async def test_auto_heal_releases_quarantine(self):
        agent = BaseAgent("a1", "test")
        orch = ImmuneSystemOrchestrator([agent])
        _feed_normal_vitals(orch, agent, n=20)

        infection = InfectionReport(
            agent_id="a1",
            max_deviation=3.0,
            anomalies=[AnomalyType.LATENCY_SPIKE],
            deviations={"latency": 3.0},
        )
        orch.quarantine.quarantine("a1")
        agent.quarantine()

        await orch.heal_agent("a1", infection)
        # After healing, the agent should be released
        assert not orch.quarantine.is_quarantined("a1")


class TestDeviationThresholdSplit:
    def test_mild_deviation_is_auto_heal(self):
        assert 3.0 < DEVIATION_REQUIRING_APPROVAL

    def test_severe_deviation_requires_approval(self):
        assert DEVIATION_REQUIRING_APPROVAL >= 5.0


class TestStatistics:
    def test_summary_counts(self):
        agent = BaseAgent("a1", "test")
        orch = ImmuneSystemOrchestrator([agent])
        _feed_normal_vitals(orch, agent, n=20)
        assert orch.total_infections == 0
        assert orch.total_healed == 0
