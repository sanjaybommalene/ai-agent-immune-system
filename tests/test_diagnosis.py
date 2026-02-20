"""Tests for Diagnostician rule-based diagnosis."""
import pytest

from immune_system.detection import AnomalyType, InfectionReport
from immune_system.diagnosis import Diagnostician, DiagnosisType


@pytest.fixture
def diagnostician():
    return Diagnostician()


def _make_infection(anomalies, deviations=None, agent_id="a1", max_dev=5.0):
    devs = deviations or {a.value: max_dev for a in anomalies}
    return InfectionReport(
        agent_id=agent_id,
        max_deviation=max_dev,
        anomalies=anomalies,
        deviations=devs,
    )


class TestPromptInjection:
    def test_prompt_change_with_input_spike(self, diagnostician, learned_baseline):
        inf = _make_infection([AnomalyType.PROMPT_CHANGE, AnomalyType.INPUT_TOKEN_SPIKE])
        diag = diagnostician.diagnose(inf, learned_baseline).primary
        assert diag.diagnosis_type == DiagnosisType.PROMPT_INJECTION
        assert diag.confidence >= 0.90

    def test_prompt_change_alone(self, diagnostician, learned_baseline):
        inf = _make_infection([AnomalyType.PROMPT_CHANGE])
        diag = diagnostician.diagnose(inf, learned_baseline).primary
        assert diag.diagnosis_type == DiagnosisType.PROMPT_INJECTION

    def test_input_spike_high_deviation(self, diagnostician, learned_baseline):
        inf = _make_infection(
            [AnomalyType.INPUT_TOKEN_SPIKE],
            deviations={"input_tokens": 4.0},
        )
        diag = diagnostician.diagnose(inf, learned_baseline).primary
        assert diag.diagnosis_type == DiagnosisType.PROMPT_INJECTION


class TestPromptDrift:
    def test_output_token_spike(self, diagnostician, learned_baseline):
        inf = _make_infection(
            [AnomalyType.OUTPUT_TOKEN_SPIKE],
            deviations={"output_tokens": 4.0},
        )
        diag = diagnostician.diagnose(inf, learned_baseline).primary
        assert diag.diagnosis_type == DiagnosisType.PROMPT_DRIFT

    def test_token_spike(self, diagnostician, learned_baseline):
        inf = _make_infection(
            [AnomalyType.TOKEN_SPIKE],
            deviations={"tokens": 4.0},
        )
        diag = diagnostician.diagnose(inf, learned_baseline).primary
        assert diag.diagnosis_type == DiagnosisType.PROMPT_DRIFT


class TestCostOverrun:
    def test_cost_spike(self, diagnostician, learned_baseline):
        inf = _make_infection([AnomalyType.COST_SPIKE], deviations={"cost": 3.5})
        diag = diagnostician.diagnose(inf, learned_baseline).primary
        assert diag.diagnosis_type == DiagnosisType.COST_OVERRUN


class TestInfiniteLoop:
    def test_tool_explosion(self, diagnostician, learned_baseline):
        inf = _make_infection(
            [AnomalyType.TOOL_EXPLOSION],
            deviations={"tools": 4.0},
        )
        diag = diagnostician.diagnose(inf, learned_baseline).primary
        assert diag.diagnosis_type == DiagnosisType.INFINITE_LOOP


class TestToolInstability:
    def test_latency_with_retries(self, diagnostician, learned_baseline):
        inf = _make_infection(
            [AnomalyType.LATENCY_SPIKE, AnomalyType.HIGH_RETRY_RATE],
            deviations={"latency": 3.0, "retry_rate": 3.0},
        )
        diag = diagnostician.diagnose(inf, learned_baseline).primary
        assert diag.diagnosis_type == DiagnosisType.TOOL_INSTABILITY

    def test_error_rate_spike_alone(self, diagnostician, learned_baseline):
        inf = _make_infection(
            [AnomalyType.ERROR_RATE_SPIKE],
            deviations={"error_rate": 3.0},
        )
        diag = diagnostician.diagnose(inf, learned_baseline).primary
        assert diag.diagnosis_type == DiagnosisType.TOOL_INSTABILITY

    def test_latency_spike_alone(self, diagnostician, learned_baseline):
        inf = _make_infection(
            [AnomalyType.LATENCY_SPIKE],
            deviations={"latency": 3.0},
        )
        diag = diagnostician.diagnose(inf, learned_baseline).primary
        assert diag.diagnosis_type == DiagnosisType.TOOL_INSTABILITY


class TestMemoryCorruption:
    def test_high_retry_rate_alone(self, diagnostician, learned_baseline):
        inf = _make_infection(
            [AnomalyType.HIGH_RETRY_RATE],
            deviations={"retry_rate": 3.0},
        )
        diag = diagnostician.diagnose(inf, learned_baseline).primary
        assert diag.diagnosis_type == DiagnosisType.MEMORY_CORRUPTION


class TestUnknownFallback:
    def test_empty_anomalies(self, diagnostician, learned_baseline):
        inf = _make_infection([], deviations={}, max_dev=0.0)
        diag = diagnostician.diagnose(inf, learned_baseline).primary
        assert diag.diagnosis_type == DiagnosisType.UNKNOWN
        assert diag.confidence < 0.5


class TestConfidence:
    def test_all_diagnoses_have_valid_confidence(self, diagnostician, learned_baseline):
        cases = [
            [AnomalyType.PROMPT_CHANGE, AnomalyType.INPUT_TOKEN_SPIKE],
            [AnomalyType.TOKEN_SPIKE],
            [AnomalyType.COST_SPIKE],
            [AnomalyType.TOOL_EXPLOSION],
            [AnomalyType.LATENCY_SPIKE, AnomalyType.HIGH_RETRY_RATE],
            [AnomalyType.HIGH_RETRY_RATE],
        ]
        for anomalies in cases:
            inf = _make_infection(anomalies, deviations={a.value: 4.0 for a in anomalies})
            diag = diagnostician.diagnose(inf, learned_baseline).primary
            assert 0.0 <= diag.confidence <= 1.0
