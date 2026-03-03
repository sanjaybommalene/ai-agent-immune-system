"""Tests for Sentinel anomaly detection."""
import pytest

from immune_system.detection import (
    AnomalyType,
    InfectionReport,
    Sentinel,
    _safe_deviation,
    _STDDEV_FLOOR_FACTOR,
)


class TestSafeDeviation:
    def test_normal_deviation(self):
        dev = _safe_deviation(150.0, 100.0, 25.0)
        assert dev == pytest.approx(2.0)

    def test_zero_stddev_uses_floor(self):
        dev = _safe_deviation(110.0, 100.0, 0.0)
        floor = 100.0 * _STDDEV_FLOOR_FACTOR  # 5.0
        assert dev == pytest.approx(10.0 / floor)

    def test_zero_mean_and_zero_stddev_returns_none(self):
        dev = _safe_deviation(0.0, 0.0, 0.0)
        assert dev is None

    def test_value_equal_to_mean_returns_zero(self):
        dev = _safe_deviation(100.0, 100.0, 10.0)
        assert dev == pytest.approx(0.0)


class TestNoInfection:
    def test_within_baseline_no_infection(self, sample_vitals, learned_baseline):
        sentinel = Sentinel(threshold_stddev=2.5)
        vitals = [sample_vitals() for _ in range(5)]
        result = sentinel.detect_infection(vitals, learned_baseline)
        assert result is None

    def test_empty_vitals_no_infection(self, learned_baseline):
        sentinel = Sentinel()
        assert sentinel.detect_infection([], learned_baseline) is None

    def test_no_baseline_no_infection(self, sample_vitals):
        sentinel = Sentinel()
        assert sentinel.detect_infection([sample_vitals()], None) is None


class TestLatencySpike:
    def test_latency_spike_detected(self, sample_vitals, learned_baseline):
        sentinel = Sentinel(threshold_stddev=2.5)
        vitals = [sample_vitals(latency_ms=500) for _ in range(5)]
        result = sentinel.detect_infection(vitals, learned_baseline)
        assert result is not None
        assert AnomalyType.LATENCY_SPIKE in result.anomalies
        assert result.max_deviation > 2.5


class TestTokenSpike:
    def test_token_spike_detected(self, sample_vitals, learned_baseline):
        sentinel = Sentinel(threshold_stddev=2.5)
        vitals = [sample_vitals(token_count=1500) for _ in range(5)]
        result = sentinel.detect_infection(vitals, learned_baseline)
        assert result is not None
        assert AnomalyType.TOKEN_SPIKE in result.anomalies


class TestToolExplosion:
    def test_tool_explosion_detected(self, sample_vitals, learned_baseline):
        sentinel = Sentinel(threshold_stddev=2.5)
        vitals = [sample_vitals(tool_calls=20) for _ in range(5)]
        result = sentinel.detect_infection(vitals, learned_baseline)
        assert result is not None
        assert AnomalyType.TOOL_EXPLOSION in result.anomalies


class TestCostSpike:
    def test_cost_spike_detected(self, sample_vitals, learned_baseline):
        sentinel = Sentinel(threshold_stddev=2.5)
        vitals = [sample_vitals(cost=0.05) for _ in range(5)]
        result = sentinel.detect_infection(vitals, learned_baseline)
        assert result is not None
        assert AnomalyType.COST_SPIKE in result.anomalies


class TestRetryRate:
    def test_high_retry_rate_detected(self, sample_vitals, learned_baseline):
        sentinel = Sentinel(threshold_stddev=2.5)
        vitals = [sample_vitals(retries=3) for _ in range(5)]
        result = sentinel.detect_infection(vitals, learned_baseline)
        assert result is not None
        assert AnomalyType.HIGH_RETRY_RATE in result.anomalies


class TestErrorRate:
    def test_error_rate_spike_detected(self, sample_vitals, learned_baseline):
        sentinel = Sentinel(threshold_stddev=2.5)
        vitals = [sample_vitals(error_type="timeout", success=False) for _ in range(5)]
        result = sentinel.detect_infection(vitals, learned_baseline)
        assert result is not None
        assert AnomalyType.ERROR_RATE_SPIKE in result.anomalies


class TestPromptChange:
    def test_prompt_change_detected(self, sample_vitals, learned_baseline):
        sentinel = Sentinel(threshold_stddev=2.5)
        vitals = [sample_vitals(prompt_hash="new-hash-xyz") for _ in range(5)]
        result = sentinel.detect_infection(vitals, learned_baseline)
        assert result is not None
        assert AnomalyType.PROMPT_CHANGE in result.anomalies

    def test_no_prompt_change_when_minority(self, sample_vitals, learned_baseline):
        sentinel = Sentinel(threshold_stddev=2.5)
        vitals = [sample_vitals() for _ in range(4)] + [sample_vitals(prompt_hash="new")]
        result = sentinel.detect_infection(vitals, learned_baseline)
        # Prompt change should NOT fire (only 1 out of 5)
        if result is not None:
            assert AnomalyType.PROMPT_CHANGE not in result.anomalies


class TestMaxDeviation:
    def test_max_deviation_is_largest(self, sample_vitals, learned_baseline):
        sentinel = Sentinel(threshold_stddev=2.5)
        vitals = [sample_vitals(latency_ms=500, token_count=1500) for _ in range(5)]
        result = sentinel.detect_infection(vitals, learned_baseline)
        assert result is not None
        max_dev = max(result.deviations.values())
        assert result.max_deviation == pytest.approx(max_dev)


class TestInfectionReport:
    def test_max_deviation_stored(self):
        report = InfectionReport(
            agent_id="a1",
            max_deviation=5.0,
            anomalies=[AnomalyType.TOKEN_SPIKE],
            deviations={"tokens": 5.0},
        )
        assert report.max_deviation == 5.0

    def test_str_includes_max_deviation(self):
        report = InfectionReport("a1", max_deviation=3.5, anomalies=[], deviations={})
        s = str(report)
        assert "3.5" in s
        assert "σ" in s or "max_dev" in s


class TestStddevFloorDetection:
    def test_constant_baseline_detects_change(self, sample_vitals):
        """When baseline has zero stddev (constant), the floor still detects anomalies."""
        from immune_system.baseline import BaselineProfile
        constant_baseline = BaselineProfile(
            agent_id="a1",
            latency_mean=100.0, latency_stddev=0.0, latency_p95=100.0,
            tokens_mean=300.0, tokens_stddev=0.0, tokens_p95=300.0,
            tools_mean=2.0, tools_stddev=0.0, tools_p95=2.0,
            sample_size=50,
        )
        sentinel = Sentinel(threshold_stddev=2.5)
        # 120ms is 20 above mean of 100; floor = 5.0, deviation = 4.0 > 2.5
        vitals = [sample_vitals(latency_ms=120)] * 5
        result = sentinel.detect_infection(vitals, constant_baseline)
        assert result is not None
        assert AnomalyType.LATENCY_SPIKE in result.anomalies
