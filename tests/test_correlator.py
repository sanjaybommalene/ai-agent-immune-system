"""Tests for immune_system.correlator — fleet-wide anomaly correlation."""
import pytest
from unittest.mock import MagicMock
from immune_system.correlator import (
    CorrelationVerdict,
    FleetCorrelator,
)
from immune_system.detection import AnomalyType, InfectionReport, Sentinel
from immune_system.baseline import BaselineProfile


def _make_baseline(agent_id: str) -> BaselineProfile:
    return BaselineProfile(
        agent_id=agent_id,
        latency_mean=300.0, latency_stddev=50.0, latency_p95=400.0,
        tokens_mean=1200.0, tokens_stddev=200.0, tokens_p95=1500.0,
        tools_mean=3.0, tools_stddev=1.0, tools_p95=5.0,
        sample_size=50,
    )


def _make_infection(agent_id: str, anomalies=None):
    anoms = anomalies or [AnomalyType.LATENCY_SPIKE]
    return InfectionReport(
        agent_id=agent_id,
        max_deviation=4.0,
        anomalies=anoms,
        deviations={a.value: 4.0 for a in anoms},
    )


class TestFleetCorrelator:
    def test_agent_specific_when_no_other_agents(self):
        fc = FleetCorrelator()
        infection = _make_infection("a1")
        sentinel = MagicMock()
        telemetry = MagicMock()
        telemetry.get_recent.return_value = []

        result = fc.correlate(infection, {}, sentinel, {}, telemetry)
        assert result.verdict == CorrelationVerdict.AGENT_SPECIFIC

    def test_agent_specific_when_no_overlap(self):
        fc = FleetCorrelator()
        infection = _make_infection("a1")
        sentinel = MagicMock()
        sentinel.detect_infection.return_value = None

        telemetry = MagicMock()
        telemetry.get_recent.return_value = [MagicMock()]

        agents = {"a1": MagicMock(), "a2": MagicMock(), "a3": MagicMock()}
        baselines = {
            "a2": _make_baseline("a2"),
            "a3": _make_baseline("a3"),
        }

        result = fc.correlate(infection, agents, sentinel, baselines, telemetry)
        assert result.verdict == CorrelationVerdict.AGENT_SPECIFIC

    def test_fleet_wide_when_many_affected(self):
        fc = FleetCorrelator(fleet_wide_threshold=0.30)
        infection = _make_infection("a1", [AnomalyType.LATENCY_SPIKE])

        other_infection = InfectionReport(
            agent_id="other",
            max_deviation=3.5,
            anomalies=[AnomalyType.LATENCY_SPIKE],
            deviations={"latency": 3.5},
        )
        sentinel = MagicMock()
        sentinel.detect_infection.return_value = other_infection

        telemetry = MagicMock()
        telemetry.get_recent.return_value = [MagicMock()]

        agents = {f"a{i}": MagicMock() for i in range(1, 12)}
        baselines = {f"a{i}": _make_baseline(f"a{i}") for i in range(2, 12)}

        result = fc.correlate(infection, agents, sentinel, baselines, telemetry)
        assert result.verdict == CorrelationVerdict.FLEET_WIDE
        assert result.affected_fraction > 0.3

    def test_partial_fleet(self):
        fc = FleetCorrelator(fleet_wide_threshold=0.50, partial_threshold=0.15)
        infection = _make_infection("a1")

        other_infection = InfectionReport(
            agent_id="other", max_deviation=3.0,
            anomalies=[AnomalyType.LATENCY_SPIKE],
            deviations={"latency": 3.0},
        )

        call_count = [0]
        def mock_detect(recent, baseline):
            call_count[0] += 1
            if call_count[0] <= 2:
                return other_infection
            return None

        sentinel = MagicMock()
        sentinel.detect_infection.side_effect = mock_detect

        telemetry = MagicMock()
        telemetry.get_recent.return_value = [MagicMock()]

        agents = {f"a{i}": MagicMock() for i in range(1, 11)}
        baselines = {f"a{i}": _make_baseline(f"a{i}") for i in range(2, 11)}

        result = fc.correlate(infection, agents, sentinel, baselines, telemetry)
        assert result.verdict == CorrelationVerdict.PARTIAL_FLEET

    def test_no_overlap_different_anomalies(self):
        fc = FleetCorrelator()
        infection = _make_infection("a1", [AnomalyType.TOKEN_SPIKE])

        other = InfectionReport(
            agent_id="other", max_deviation=3.0,
            anomalies=[AnomalyType.LATENCY_SPIKE],
            deviations={"latency": 3.0},
        )
        sentinel = MagicMock()
        sentinel.detect_infection.return_value = other

        telemetry = MagicMock()
        telemetry.get_recent.return_value = [MagicMock()]

        agents = {"a1": MagicMock(), "a2": MagicMock(), "a3": MagicMock()}
        baselines = {"a2": _make_baseline("a2"), "a3": _make_baseline("a3")}

        result = fc.correlate(infection, agents, sentinel, baselines, telemetry)
        assert result.verdict == CorrelationVerdict.AGENT_SPECIFIC
