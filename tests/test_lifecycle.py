"""Tests for immune_system.lifecycle — 8-state agent lifecycle."""
import pytest
from immune_system.lifecycle import (
    AgentPhase,
    LifecycleManager,
    TransitionEvent,
)


@pytest.fixture
def lm():
    return LifecycleManager(suspect_ticks=3, drain_timeout_s=2.0, probation_ticks=5)


class TestInitialState:
    def test_default_phase_is_initializing(self, lm):
        assert lm.get_phase("a1") == AgentPhase.INITIALIZING

    def test_is_execution_allowed_for_initializing(self, lm):
        assert lm.is_execution_allowed("a1") is True


class TestBaselineReady:
    def test_transition_to_healthy(self, lm):
        assert lm.mark_baseline_ready("a1") is True
        assert lm.get_phase("a1") == AgentPhase.HEALTHY

    def test_cannot_skip_to_suspected(self, lm):
        assert lm.transition("a1", AgentPhase.SUSPECTED, "test") is False


class TestSuspectedEscalation:
    def test_single_anomaly_enters_suspected(self, lm):
        lm.mark_baseline_ready("a1")
        phase = lm.record_anomaly_tick("a1")
        assert phase == AgentPhase.SUSPECTED

    def test_anomaly_resolves(self, lm):
        lm.mark_baseline_ready("a1")
        lm.record_anomaly_tick("a1")
        assert lm.record_anomaly_resolved("a1") is True
        assert lm.get_phase("a1") == AgentPhase.HEALTHY

    def test_anomaly_persists_to_draining(self, lm):
        lm.mark_baseline_ready("a1")
        lm.record_anomaly_tick("a1")
        lm.record_anomaly_tick("a1")
        phase = lm.record_anomaly_tick("a1")
        assert phase == AgentPhase.DRAINING


class TestDraining:
    def test_complete_drain(self, lm):
        lm.mark_baseline_ready("a1")
        lm.force_drain("a1", "test")
        assert lm.get_phase("a1") == AgentPhase.DRAINING
        assert lm.complete_drain("a1") is True
        assert lm.get_phase("a1") == AgentPhase.QUARANTINED

    def test_drain_timeout(self, lm):
        import time
        lm2 = LifecycleManager(drain_timeout_s=0.01)
        lm2.mark_baseline_ready("a1")
        lm2.force_drain("a1", "test")
        time.sleep(0.02)
        assert lm2.check_drain_timeout("a1") is True


class TestForceDrain:
    def test_severe_anomaly_skips_suspected(self, lm):
        lm.mark_baseline_ready("a1")
        assert lm.force_drain("a1", "severe") is True
        assert lm.get_phase("a1") == AgentPhase.DRAINING


class TestHealingFlow:
    def test_quarantine_to_healing(self, lm):
        lm.mark_baseline_ready("a1")
        lm.force_drain("a1")
        lm.complete_drain("a1")
        assert lm.start_healing("a1") is True
        assert lm.get_phase("a1") == AgentPhase.HEALING

    def test_healing_to_probation(self, lm):
        lm.mark_baseline_ready("a1")
        lm.force_drain("a1")
        lm.complete_drain("a1")
        lm.start_healing("a1")
        assert lm.enter_probation("a1") is True
        assert lm.get_phase("a1") == AgentPhase.PROBATION


class TestProbation:
    def test_probation_tick_counting(self, lm):
        lm.mark_baseline_ready("a1")
        lm.force_drain("a1")
        lm.complete_drain("a1")
        lm.start_healing("a1")
        lm.enter_probation("a1")

        for i in range(4):
            count = lm.record_probation_tick("a1")
            assert count == i + 1
            assert lm.probation_complete("a1") is False

        lm.record_probation_tick("a1")
        assert lm.probation_complete("a1") is True

    def test_probation_to_healthy(self, lm):
        lm.mark_baseline_ready("a1")
        lm.force_drain("a1")
        lm.complete_drain("a1")
        lm.start_healing("a1")
        lm.enter_probation("a1")
        for _ in range(5):
            lm.record_probation_tick("a1")
        assert lm.mark_healthy("a1") is True
        assert lm.get_phase("a1") == AgentPhase.HEALTHY

    def test_probation_to_healing_on_failure(self, lm):
        lm.mark_baseline_ready("a1")
        lm.force_drain("a1")
        lm.complete_drain("a1")
        lm.start_healing("a1")
        lm.enter_probation("a1")
        assert lm.transition("a1", AgentPhase.HEALING, "validation_failed") is True
        assert lm.get_phase("a1") == AgentPhase.HEALING


class TestExhausted:
    def test_healing_to_exhausted(self, lm):
        lm.mark_baseline_ready("a1")
        lm.force_drain("a1")
        lm.complete_drain("a1")
        lm.start_healing("a1")
        assert lm.mark_exhausted("a1") is True
        assert lm.get_phase("a1") == AgentPhase.EXHAUSTED

    def test_exhausted_to_healing(self, lm):
        lm.mark_baseline_ready("a1")
        lm.force_drain("a1")
        lm.complete_drain("a1")
        lm.start_healing("a1")
        lm.mark_exhausted("a1")
        assert lm.start_healing("a1") is True
        assert lm.get_phase("a1") == AgentPhase.HEALING


class TestBlockedPhases:
    def test_quarantined_blocks_execution(self, lm):
        lm.mark_baseline_ready("a1")
        lm.force_drain("a1")
        lm.complete_drain("a1")
        assert lm.is_blocked("a1") is True
        assert lm.is_execution_allowed("a1") is False

    def test_healthy_allows_execution(self, lm):
        lm.mark_baseline_ready("a1")
        assert lm.is_execution_allowed("a1") is True

    def test_probation_allows_execution(self, lm):
        lm.mark_baseline_ready("a1")
        lm.force_drain("a1")
        lm.complete_drain("a1")
        lm.start_healing("a1")
        lm.enter_probation("a1")
        assert lm.is_execution_allowed("a1") is True


class TestHistory:
    def test_transitions_logged(self, lm):
        lm.mark_baseline_ready("a1")
        lm.record_anomaly_tick("a1")
        history = lm.get_history("a1")
        assert len(history) == 2
        assert all(isinstance(e, TransitionEvent) for e in history)
        assert history[0].to_phase == AgentPhase.HEALTHY

    def test_callback_invoked(self):
        events = []
        lm = LifecycleManager(on_transition=lambda e: events.append(e))
        lm.mark_baseline_ready("a1")
        assert len(events) == 1
        assert events[0].to_phase == AgentPhase.HEALTHY


class TestInvalidTransitions:
    def test_cannot_go_from_healthy_to_quarantined(self, lm):
        lm.mark_baseline_ready("a1")
        assert lm.transition("a1", AgentPhase.QUARANTINED, "test") is False

    def test_cannot_go_from_healing_to_healthy(self, lm):
        lm.mark_baseline_ready("a1")
        lm.force_drain("a1")
        lm.complete_drain("a1")
        lm.start_healing("a1")
        assert lm.transition("a1", AgentPhase.HEALTHY, "test") is False


class TestReset:
    def test_reset_clears_state(self, lm):
        lm.mark_baseline_ready("a1")
        lm.reset("a1")
        assert lm.get_phase("a1") == AgentPhase.INITIALIZING
