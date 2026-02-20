"""Tests for probation-based post-healing validation and baseline adaptation."""
import asyncio
from unittest.mock import MagicMock, AsyncMock, patch

import pytest

from immune_system.baseline import BaselineLearner, BaselineProfile, _AgentEWMA
from immune_system.detection import AnomalyType, InfectionReport
from immune_system.healing import Healer, HealingAction, HealingResult
from immune_system.lifecycle import AgentPhase, LifecycleManager


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_baseline(agent_id: str = "a1") -> BaselineProfile:
    return BaselineProfile(
        agent_id=agent_id,
        latency_mean=300.0, latency_stddev=50.0, latency_p95=400.0,
        tokens_mean=1200.0, tokens_stddev=200.0, tokens_p95=1500.0,
        tools_mean=3.0, tools_stddev=1.0, tools_p95=5.0,
        sample_size=50,
    )


def _make_vitals(**overrides):
    defaults = dict(
        latency_ms=300.0, token_count=1200, tool_calls=3,
        input_tokens=500, output_tokens=700, cost=0.01,
        retries=0, error_type="", prompt_hash="abc123",
    )
    defaults.update(overrides)
    v = MagicMock()
    for k, val in defaults.items():
        setattr(v, k, val)
    return v


# ---------------------------------------------------------------------------
# Probation Validation via Healer.validate_probation
# ---------------------------------------------------------------------------

class TestValidateProbation:
    @pytest.mark.asyncio
    async def test_passes_when_no_baseline(self):
        bl = MagicMock()
        bl.get_baseline.return_value = None
        healer = Healer(None, bl, None)
        assert await healer.validate_probation("a1") is True

    @pytest.mark.asyncio
    async def test_passes_when_insufficient_recent_data(self):
        bl = MagicMock()
        bl.get_baseline.return_value = _make_baseline()
        tc = MagicMock()
        tc.get_recent.return_value = [_make_vitals()]
        healer = Healer(tc, bl, None)
        assert await healer.validate_probation("a1") is True

    @pytest.mark.asyncio
    async def test_passes_when_sentinel_sees_no_infection(self):
        bl = MagicMock()
        bl.get_baseline.return_value = _make_baseline()
        tc = MagicMock()
        tc.get_recent.return_value = [_make_vitals(), _make_vitals(), _make_vitals()]
        sentinel = MagicMock()
        sentinel.detect_infection.return_value = None
        healer = Healer(tc, bl, sentinel)
        assert await healer.validate_probation("a1") is True

    @pytest.mark.asyncio
    async def test_fails_when_sentinel_detects_infection(self):
        bl = MagicMock()
        bl.get_baseline.return_value = _make_baseline()
        tc = MagicMock()
        tc.get_recent.return_value = [_make_vitals(), _make_vitals(), _make_vitals()]
        infection = InfectionReport(
            agent_id="a1", max_deviation=5.0,
            anomalies=[AnomalyType.TOKEN_SPIKE],
            deviations={"tokens": 5.0},
        )
        sentinel = MagicMock()
        sentinel.detect_infection.return_value = infection
        healer = Healer(tc, bl, sentinel)
        assert await healer.validate_probation("a1") is False


# ---------------------------------------------------------------------------
# LifecycleManager probation states
# ---------------------------------------------------------------------------

class TestLifecycleProbation:
    def test_enter_probation_from_healing(self):
        lm = LifecycleManager()
        lm.transition("a1", AgentPhase.HEALTHY, "init")
        lm.transition("a1", AgentPhase.SUSPECTED, "anomaly")
        lm.transition("a1", AgentPhase.DRAINING, "escalated")
        lm.transition("a1", AgentPhase.QUARANTINED, "drained")
        lm.transition("a1", AgentPhase.HEALING, "begin")
        ok = lm.enter_probation("a1")
        assert ok is True
        assert lm.get_phase("a1") == AgentPhase.PROBATION

    def test_probation_tick_counting(self):
        lm = LifecycleManager()
        lm.transition("a1", AgentPhase.HEALTHY, "init")
        lm.transition("a1", AgentPhase.SUSPECTED, "a")
        lm.transition("a1", AgentPhase.DRAINING, "b")
        lm.transition("a1", AgentPhase.QUARANTINED, "c")
        lm.transition("a1", AgentPhase.HEALING, "d")
        lm.enter_probation("a1")
        for _ in range(lm.probation_ticks):
            lm.record_probation_tick("a1")
        assert lm.probation_complete("a1") is True

    def test_probation_not_complete_too_early(self):
        lm = LifecycleManager()
        lm.transition("a1", AgentPhase.HEALTHY, "init")
        lm.transition("a1", AgentPhase.SUSPECTED, "a")
        lm.transition("a1", AgentPhase.DRAINING, "b")
        lm.transition("a1", AgentPhase.QUARANTINED, "c")
        lm.transition("a1", AgentPhase.HEALING, "d")
        lm.enter_probation("a1")
        lm.record_probation_tick("a1")
        assert lm.probation_complete("a1") is False

    def test_probation_pass_to_healthy(self):
        lm = LifecycleManager()
        lm.transition("a1", AgentPhase.HEALTHY, "init")
        lm.transition("a1", AgentPhase.SUSPECTED, "a")
        lm.transition("a1", AgentPhase.DRAINING, "b")
        lm.transition("a1", AgentPhase.QUARANTINED, "c")
        lm.transition("a1", AgentPhase.HEALING, "d")
        lm.enter_probation("a1")
        lm.mark_healthy("a1", "probation_passed")
        assert lm.get_phase("a1") == AgentPhase.HEALTHY

    def test_probation_fail_back_to_healing(self):
        lm = LifecycleManager()
        lm.transition("a1", AgentPhase.HEALTHY, "init")
        lm.transition("a1", AgentPhase.SUSPECTED, "a")
        lm.transition("a1", AgentPhase.DRAINING, "b")
        lm.transition("a1", AgentPhase.QUARANTINED, "c")
        lm.transition("a1", AgentPhase.HEALING, "d")
        lm.enter_probation("a1")
        ok = lm.transition("a1", AgentPhase.HEALING, "probation_failed")
        assert ok is True
        assert lm.get_phase("a1") == AgentPhase.HEALING

    def test_execution_allowed_during_probation(self):
        lm = LifecycleManager()
        lm.transition("a1", AgentPhase.HEALTHY, "init")
        lm.transition("a1", AgentPhase.SUSPECTED, "a")
        lm.transition("a1", AgentPhase.DRAINING, "b")
        lm.transition("a1", AgentPhase.QUARANTINED, "c")
        lm.transition("a1", AgentPhase.HEALING, "d")
        lm.enter_probation("a1")
        assert lm.is_execution_allowed("a1") is True


# ---------------------------------------------------------------------------
# Baseline adaptation post-healing
# ---------------------------------------------------------------------------

class TestBaselineAdaptation:
    def test_reset_baseline_clears_state(self):
        bl = BaselineLearner(min_samples=3)
        for _ in range(5):
            bl.update("a1", _make_vitals())
        assert bl.has_baseline("a1")
        bl.reset_baseline("a1")
        assert not bl.has_baseline("a1")

    def test_accelerate_learning_changes_alpha(self):
        bl = BaselineLearner(min_samples=3)
        for _ in range(5):
            bl.update("a1", _make_vitals())
        original_alpha = bl.alpha
        bl.accelerate_learning("a1", ticks=10)
        ewma = bl._ewma["a1"]
        assert ewma.latency.alpha > original_alpha

    def test_deceleration_reverts_after_ticks(self):
        bl = BaselineLearner(min_samples=3)
        for _ in range(5):
            bl.update("a1", _make_vitals())
        original_alpha = bl.alpha
        bl.accelerate_learning("a1", ticks=5)
        ewma = bl._ewma["a1"]
        fast_alpha = ewma.latency.alpha
        assert fast_alpha > original_alpha
        for _ in range(6):
            bl.update("a1", _make_vitals())
        assert ewma.latency.alpha == original_alpha

    def test_accelerate_noop_if_no_ewma(self):
        bl = BaselineLearner(min_samples=3)
        bl.accelerate_learning("unknown_agent", ticks=10)
        assert "unknown_agent" not in bl._pending_deceleration

    def test_reset_then_relearn(self):
        bl = BaselineLearner(min_samples=3)
        for _ in range(5):
            bl.update("a1", _make_vitals(latency_ms=300.0))
        old_baseline = bl.get_baseline("a1")
        assert old_baseline is not None

        bl.reset_baseline("a1")
        assert bl.get_baseline("a1") is None

        for _ in range(5):
            bl.update("a1", _make_vitals(latency_ms=150.0))
        new_baseline = bl.get_baseline("a1")
        assert new_baseline is not None
        assert new_baseline.latency_mean < old_baseline.latency_mean
