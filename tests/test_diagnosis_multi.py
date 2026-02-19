"""Tests for multi-hypothesis diagnosis and success-weighted healing."""
import pytest
from immune_system.detection import AnomalyType, InfectionReport
from immune_system.diagnosis import (
    Diagnosis,
    DiagnosisContext,
    DiagnosisFeedback,
    DiagnosisResult,
    DiagnosisType,
    Diagnostician,
)
from immune_system.healing import Healer, HealingAction, HEALING_POLICIES
from immune_system.memory import ImmuneMemory
from immune_system.baseline import BaselineProfile


def _make_baseline(agent_id: str = "a1") -> BaselineProfile:
    return BaselineProfile(
        agent_id=agent_id,
        latency_mean=300.0, latency_stddev=50.0, latency_p95=400.0,
        tokens_mean=1200.0, tokens_stddev=200.0, tokens_p95=1500.0,
        tools_mean=3.0, tools_stddev=1.0, tools_p95=5.0,
        sample_size=50,
    )


class TestMultiHypothesisDiagnosis:
    def test_returns_diagnosis_result(self):
        d = Diagnostician()
        infection = InfectionReport(
            agent_id="a1", max_deviation=5.0,
            anomalies=[AnomalyType.TOKEN_SPIKE, AnomalyType.PROMPT_CHANGE],
            deviations={"tokens": 5.0, "prompt_change": 10.0},
        )
        result = d.diagnose(infection, _make_baseline())
        assert isinstance(result, DiagnosisResult)
        assert len(result.hypotheses) >= 1
        assert result.primary.agent_id == "a1"

    def test_multiple_hypotheses_for_prompt_change(self):
        d = Diagnostician()
        infection = InfectionReport(
            agent_id="a1", max_deviation=5.0,
            anomalies=[AnomalyType.PROMPT_CHANGE, AnomalyType.INPUT_TOKEN_SPIKE],
            deviations={"prompt_change": 10.0, "input_tokens": 4.0},
        )
        result = d.diagnose(infection, _make_baseline())
        types = [h.diagnosis_type for h in result.hypotheses]
        assert DiagnosisType.PROMPT_INJECTION in types
        assert DiagnosisType.PROMPT_DRIFT in types

    def test_primary_has_highest_confidence(self):
        d = Diagnostician()
        infection = InfectionReport(
            agent_id="a1", max_deviation=5.0,
            anomalies=[AnomalyType.PROMPT_CHANGE],
            deviations={"prompt_change": 10.0},
        )
        result = d.diagnose(infection, _make_baseline())
        confidences = [h.confidence for h in result.hypotheses]
        assert confidences == sorted(confidences, reverse=True)

    def test_fleet_wide_adds_external_cause(self):
        d = Diagnostician()
        infection = InfectionReport(
            agent_id="a1", max_deviation=3.0,
            anomalies=[AnomalyType.LATENCY_SPIKE],
            deviations={"latency": 3.0},
        )
        ctx = DiagnosisContext(fleet_wide=True, affected_fraction=0.45)
        result = d.diagnose(infection, _make_baseline(), context=ctx)
        types = [h.diagnosis_type for h in result.hypotheses]
        assert DiagnosisType.EXTERNAL_CAUSE in types
        assert result.primary.diagnosis_type == DiagnosisType.EXTERNAL_CAUSE

    def test_unknown_for_no_matching_patterns(self):
        d = Diagnostician()
        infection = InfectionReport(
            agent_id="a1", max_deviation=2.6,
            anomalies=[],
            deviations={},
        )
        result = d.diagnose(infection, _make_baseline())
        assert result.primary.diagnosis_type == DiagnosisType.UNKNOWN

    def test_backward_compat_diagnose_single(self):
        d = Diagnostician()
        infection = InfectionReport(
            agent_id="a1", max_deviation=5.0,
            anomalies=[AnomalyType.TOKEN_SPIKE],
            deviations={"tokens": 5.0},
        )
        single = d.diagnose_single(infection, _make_baseline())
        assert isinstance(single, Diagnosis)

    def test_dedup_removes_duplicate_types(self):
        d = Diagnostician()
        infection = InfectionReport(
            agent_id="a1", max_deviation=5.0,
            anomalies=[AnomalyType.INPUT_TOKEN_SPIKE, AnomalyType.PROMPT_CHANGE],
            deviations={"input_tokens": 4.0, "prompt_change": 10.0},
        )
        result = d.diagnose(infection, _make_baseline())
        types = [h.diagnosis_type for h in result.hypotheses]
        assert len(types) == len(set(types))


class TestOperatorFeedback:
    def test_feedback_adjusts_confidence(self):
        d = Diagnostician()
        fb = DiagnosisFeedback(
            agent_id="a1",
            original_type=DiagnosisType.TOOL_INSTABILITY,
            actual_cause="false_positive",
        )
        d.record_feedback(fb)

        infection = InfectionReport(
            agent_id="a1", max_deviation=3.0,
            anomalies=[AnomalyType.LATENCY_SPIKE, AnomalyType.ERROR_RATE_SPIKE],
            deviations={"latency": 3.0, "error_rate": 3.0},
        )
        result = d.diagnose(infection, _make_baseline())
        tool_hyp = next(
            (h for h in result.hypotheses if h.diagnosis_type == DiagnosisType.TOOL_INSTABILITY),
            None,
        )
        assert tool_hyp is not None
        assert tool_hyp.confidence < 0.75


class TestSuccessWeightedActionSelection:
    def test_default_order_without_memory(self):
        healer = Healer(None, None, None)
        action = healer.get_next_action(DiagnosisType.PROMPT_DRIFT, set())
        assert action == HealingAction.RESET_MEMORY

    def test_skips_failed_actions(self):
        healer = Healer(None, None, None)
        failed = {HealingAction.RESET_MEMORY, HealingAction.ROLLBACK_PROMPT}
        action = healer.get_next_action(DiagnosisType.PROMPT_DRIFT, failed)
        assert action == HealingAction.REDUCE_AUTONOMY

    def test_reorders_by_global_success(self):
        healer = Healer(None, None, None)
        memory = ImmuneMemory()
        memory.record_healing("x", DiagnosisType.PROMPT_DRIFT, HealingAction.ROLLBACK_PROMPT, True)
        memory.record_healing("y", DiagnosisType.PROMPT_DRIFT, HealingAction.ROLLBACK_PROMPT, True)
        memory.record_healing("z", DiagnosisType.PROMPT_DRIFT, HealingAction.RESET_MEMORY, True)

        action = healer.get_next_action(DiagnosisType.PROMPT_DRIFT, set(), memory)
        assert action == HealingAction.ROLLBACK_PROMPT

    def test_skips_failed_even_with_success_patterns(self):
        healer = Healer(None, None, None)
        memory = ImmuneMemory()
        memory.record_healing("x", DiagnosisType.PROMPT_DRIFT, HealingAction.ROLLBACK_PROMPT, True)

        failed = {HealingAction.ROLLBACK_PROMPT}
        action = healer.get_next_action(DiagnosisType.PROMPT_DRIFT, failed, memory)
        assert action != HealingAction.ROLLBACK_PROMPT
        assert action is not None

    def test_returns_none_when_all_exhausted(self):
        healer = Healer(None, None, None)
        all_actions = set(HEALING_POLICIES[DiagnosisType.PROMPT_DRIFT])
        action = healer.get_next_action(DiagnosisType.PROMPT_DRIFT, all_actions)
        assert action is None

    def test_external_cause_has_policy(self):
        healer = Healer(None, None, None)
        policy = healer.get_healing_policy(DiagnosisType.EXTERNAL_CAUSE)
        assert len(policy) > 0


class TestImmuneMemoryCrossAgent:
    def test_global_success_patterns_across_agents(self):
        memory = ImmuneMemory()
        memory.record_healing("a1", DiagnosisType.PROMPT_DRIFT, HealingAction.ROLLBACK_PROMPT, True)
        memory.record_healing("a2", DiagnosisType.PROMPT_DRIFT, HealingAction.ROLLBACK_PROMPT, True)
        memory.record_healing("a3", DiagnosisType.PROMPT_DRIFT, HealingAction.RESET_MEMORY, True)

        successful = memory.get_successful_actions(DiagnosisType.PROMPT_DRIFT)
        assert successful[0] == HealingAction.ROLLBACK_PROMPT
        assert HealingAction.RESET_MEMORY in successful

    def test_success_rate_for_action(self):
        memory = ImmuneMemory()
        memory.record_healing("a1", DiagnosisType.INFINITE_LOOP, HealingAction.REVOKE_TOOLS, True)
        memory.record_healing("a2", DiagnosisType.INFINITE_LOOP, HealingAction.REVOKE_TOOLS, True)
        memory.record_healing("a3", DiagnosisType.INFINITE_LOOP, HealingAction.REVOKE_TOOLS, False)

        rate = memory.get_success_rate_for_action(DiagnosisType.INFINITE_LOOP, HealingAction.REVOKE_TOOLS)
        assert abs(rate - 2/3) < 0.01

    def test_feedback_storage(self):
        memory = ImmuneMemory()
        fb = DiagnosisFeedback(
            agent_id="a1",
            original_type=DiagnosisType.TOOL_INSTABILITY,
            actual_cause="provider_outage",
        )
        memory.record_feedback(fb)
        assert len(memory.get_feedback_history()) == 1
