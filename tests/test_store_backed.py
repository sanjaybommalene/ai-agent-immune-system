"""Store-backed detection and run_id isolation tests using InMemoryStore."""
import time

import pytest

from immune_system.agents import BaseAgent
from immune_system.telemetry import AgentVitals, TelemetryCollector
from immune_system.baseline import BaselineLearner
from immune_system.detection import Sentinel, AnomalyType
from immune_system.orchestrator import ImmuneSystemOrchestrator

from tests.store_helpers import InMemoryStore


def _vitals_dict(agent_id="a1", **overrides):
    base = {
        "timestamp": time.time(),
        "agent_id": agent_id,
        "agent_type": "test",
        "latency_ms": 120,
        "token_count": 300,
        "tool_calls": 2,
        "retries": 0,
        "success": True,
        "input_tokens": 150,
        "output_tokens": 150,
        "cost": 0.002,
        "model": "gpt-4o",
        "error_type": "",
        "prompt_hash": "abc123",
    }
    base.update(overrides)
    return base


class TestStoreBackedDetection:
    """When store is set, vitals flow to store and sentinel reads from store."""

    def test_detection_using_vitals_from_store(self):
        run_id = "run-store-detection"
        store = InMemoryStore(run_id=run_id)
        telemetry = TelemetryCollector(store=store)
        baseline_learner = BaselineLearner(min_samples=5, store=store, cache=None)
        sentinel = Sentinel(threshold_stddev=2.5)

        agent_id = "a1"
        # Feed normal vitals into telemetry (writes to store)
        for _ in range(10):
            v = _vitals_dict(agent_id=agent_id)
            telemetry.record(v)
            baseline_learner.update(agent_id, AgentVitals(**{k: v[k] for k in AgentVitals.__dataclass_fields__ if k in v}))

        baseline = baseline_learner.get_baseline(agent_id)
        assert baseline is not None

        # Spike: write anomalous vitals to store
        for _ in range(5):
            telemetry.record(_vitals_dict(agent_id=agent_id, latency_ms=800))

        # Sentinel reads recent from store via telemetry.get_recent()
        recent = telemetry.get_recent(agent_id, window_seconds=30)
        assert len(recent) >= 5
        infection = sentinel.detect_infection(recent, baseline)
        assert infection is not None
        assert AnomalyType.LATENCY_SPIKE in infection.anomalies


class TestRunIdIsolation:
    """Data written with one run_id is not visible to a store with another run_id."""

    def test_vitals_isolated_by_run_id(self):
        store_a = InMemoryStore(run_id="run-A")
        store_b = InMemoryStore(run_id="run-B")

        store_a.write_agent_vitals(_vitals_dict(agent_id="a1", latency_ms=100))
        store_a.write_agent_vitals(_vitals_dict(agent_id="a1", latency_ms=100))
        store_b.write_agent_vitals(_vitals_dict(agent_id="a1", latency_ms=200))

        recent_a = store_a.get_recent_agent_vitals("a1", window_seconds=60)
        recent_b = store_b.get_recent_agent_vitals("a1", window_seconds=60)

        assert len(recent_a) == 2
        assert len(recent_b) == 1
        assert all(r["latency_ms"] == 100 for r in recent_a)
        assert recent_b[0]["latency_ms"] == 200

    def test_orchestrator_with_store_uses_store_for_telemetry(self):
        """Orchestrator with InMemoryStore: vitals go to store; detection sees them."""
        store = InMemoryStore(run_id="run-orch")
        agent = BaseAgent("a1", "test")
        orch = ImmuneSystemOrchestrator([agent], store=store, cache=None)

        # Feed vitals (orchestrator records to telemetry -> store)
        for _ in range(20):
            v = _vitals_dict(agent_id="a1")
            orch.telemetry.record(v)
            vitals_obj = AgentVitals(**{k: v[k] for k in AgentVitals.__dataclass_fields__ if k in v})
            orch.baseline_learner.update("a1", vitals_obj)

        assert orch.baseline_learner.has_baseline("a1")
        assert store.get_total_executions() >= 20

        # Spike and get recent from store
        for _ in range(5):
            orch.telemetry.record(_vitals_dict(agent_id="a1", latency_ms=900))
        recent = orch.telemetry.get_recent("a1", window_seconds=10)
        baseline = orch.baseline_learner.get_baseline("a1")
        infection = orch.sentinel.detect_infection(recent, baseline)
        assert infection is not None


class TestRestartResilience:
    """Orchestrator with store and no cache (or fresh cache) still learns baseline."""

    def test_orchestrator_no_cache_learns_baseline_with_store(self):
        store = InMemoryStore(run_id="run-fresh")
        agent = BaseAgent("a1", "test")
        orch = ImmuneSystemOrchestrator([agent], store=store, cache=None)

        for _ in range(20):
            v = _vitals_dict(agent_id="a1")
            orch.telemetry.record(v)
            vitals_obj = AgentVitals(**{k: v[k] for k in AgentVitals.__dataclass_fields__ if k in v})
            orch.baseline_learner.update("a1", vitals_obj)

        assert orch.baseline_learner.has_baseline("a1")
        assert store.get_total_executions() == 20
