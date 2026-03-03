"""Tests for TelemetryCollector: bounded buffer, record/query."""
import time
import pytest

from immune_system.telemetry import TelemetryCollector, AgentVitals, _MAX_IN_MEMORY_SAMPLES


def _vitals_dict(agent_id="a1", **overrides):
    base = {
        "timestamp": time.time(),
        "agent_id": agent_id,
        "agent_type": "test",
        "latency_ms": 100,
        "token_count": 200,
        "tool_calls": 2,
        "retries": 0,
        "success": True,
        "input_tokens": 100,
        "output_tokens": 100,
        "cost": 0.001,
        "model": "test-model",
        "error_type": "",
        "prompt_hash": "hash1",
    }
    base.update(overrides)
    return base


class TestRecord:
    def test_record_and_get_all(self):
        tc = TelemetryCollector()
        tc.record(_vitals_dict())
        tc.record(_vitals_dict())
        assert tc.get_count("a1") == 2
        all_v = tc.get_all("a1")
        assert len(all_v) == 2
        assert all(isinstance(v, AgentVitals) for v in all_v)

    def test_total_executions_increments(self):
        tc = TelemetryCollector()
        assert tc.total_executions == 0
        tc.record(_vitals_dict())
        tc.record(_vitals_dict(agent_id="a2"))
        assert tc.total_executions == 2

    def test_token_count_default_from_io(self):
        tc = TelemetryCollector()
        d = _vitals_dict()
        del d["token_count"]
        d["input_tokens"] = 50
        d["output_tokens"] = 60
        tc.record(d)
        v = tc.get_latest("a1")
        assert v.token_count == 110


class TestBoundedBuffer:
    def test_deque_maxlen(self):
        tc = TelemetryCollector()
        for i in range(_MAX_IN_MEMORY_SAMPLES + 100):
            tc.record(_vitals_dict(timestamp=float(i)))
        assert tc.get_count("a1") == _MAX_IN_MEMORY_SAMPLES


class TestGetRecent:
    def test_filters_by_window(self):
        tc = TelemetryCollector()
        now = time.time()
        tc.record(_vitals_dict(timestamp=now - 60))  # old
        tc.record(_vitals_dict(timestamp=now - 5))  # within 10s
        tc.record(_vitals_dict(timestamp=now))
        recent = tc.get_recent("a1", window_seconds=10)
        assert len(recent) == 2

    def test_empty_agent(self):
        tc = TelemetryCollector()
        assert tc.get_recent("unknown") == []


class TestGetLatest:
    def test_returns_most_recent(self):
        tc = TelemetryCollector()
        tc.record(_vitals_dict(timestamp=1.0, latency_ms=100))
        tc.record(_vitals_dict(timestamp=2.0, latency_ms=200))
        v = tc.get_latest("a1")
        assert v.latency_ms == 200

    def test_returns_none_for_unknown(self):
        tc = TelemetryCollector()
        assert tc.get_latest("nope") is None


class TestMultipleAgents:
    def test_agents_are_isolated(self):
        tc = TelemetryCollector()
        tc.record(_vitals_dict(agent_id="a1"))
        tc.record(_vitals_dict(agent_id="a2"))
        tc.record(_vitals_dict(agent_id="a2"))
        assert tc.get_count("a1") == 1
        assert tc.get_count("a2") == 2
