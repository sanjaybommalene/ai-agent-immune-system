"""
Sentinel — Anomaly detection system.

Compares recent vitals to the agent's EWMA baseline and flags deviations.
Uses consistent stddev-based deviation for ALL metrics (including retry/error
rates).  Severity has been replaced by direct deviation thresholds.

Minimum stddev floor: when stddev == 0 (constant baseline), a floor of 5% of
the mean is used so that any non-trivial change can still be detected.
"""
from dataclasses import dataclass, field
from typing import Dict, List, Optional
from enum import Enum


class AnomalyType(Enum):
    TOKEN_SPIKE = "token_spike"
    LATENCY_SPIKE = "latency_spike"
    TOOL_EXPLOSION = "tool_explosion"
    HIGH_RETRY_RATE = "high_retry_rate"
    INPUT_TOKEN_SPIKE = "input_token_spike"
    OUTPUT_TOKEN_SPIKE = "output_token_spike"
    COST_SPIKE = "cost_spike"
    PROMPT_CHANGE = "prompt_change"
    ERROR_RATE_SPIKE = "error_rate_spike"

# Minimum stddev floor factor — prevents division by zero when baseline
# has zero variance (constant metric values during learning).
_STDDEV_FLOOR_FACTOR = 0.05


@dataclass
class InfectionReport:
    """Report of detected infection. All logic and display use max_deviation (σ)."""
    agent_id: str
    max_deviation: float
    anomalies: List[AnomalyType]
    deviations: Dict[str, float]

    def __str__(self):
        anomaly_str = ", ".join(a.value for a in self.anomalies)
        return (f"InfectionReport[{self.agent_id}]: max_dev={self.max_deviation:.2f}σ, "
                f"anomalies=[{anomaly_str}]")


def _safe_deviation(value: float, mean: float, stddev: float) -> Optional[float]:
    """Compute deviation with stddev floor.  Returns None if metric is meaningless."""
    effective_stddev = max(stddev, abs(mean) * _STDDEV_FLOOR_FACTOR)
    if effective_stddev <= 0:
        return None
    return abs(value - mean) / effective_stddev


class Sentinel:
    """Detects abnormal agent behavior via statistical deviations."""

    def __init__(self, threshold_stddev: float = 2.5):
        self.threshold = threshold_stddev

    def detect_infection(self, recent_vitals: List, baseline) -> Optional[InfectionReport]:
        if not recent_vitals or not baseline:
            return None

        sample_size = min(5, len(recent_vitals))
        recent = recent_vitals[-sample_size:]
        n = len(recent)

        avg_latency = sum(v.latency_ms for v in recent) / n
        avg_tokens = sum(v.token_count for v in recent) / n
        avg_tools = sum(v.tool_calls for v in recent) / n
        avg_input = sum(getattr(v, "input_tokens", 0) for v in recent) / n
        avg_output = sum(getattr(v, "output_tokens", 0) for v in recent) / n
        avg_cost = sum(getattr(v, "cost", 0.0) for v in recent) / n
        retry_rate = sum(1 for v in recent if v.retries > 0) / n
        error_rate = sum(1 for v in recent if getattr(v, "error_type", "")) / n

        deviations: Dict[str, float] = {}
        anomalies: List[AnomalyType] = []

        checks = [
            ("latency", avg_latency, baseline.latency_mean, baseline.latency_stddev, AnomalyType.LATENCY_SPIKE),
            ("tokens", avg_tokens, baseline.tokens_mean, baseline.tokens_stddev, AnomalyType.TOKEN_SPIKE),
            ("tools", avg_tools, baseline.tools_mean, baseline.tools_stddev, AnomalyType.TOOL_EXPLOSION),
            ("input_tokens", avg_input,
             getattr(baseline, "input_tokens_mean", 0), getattr(baseline, "input_tokens_stddev", 0),
             AnomalyType.INPUT_TOKEN_SPIKE),
            ("output_tokens", avg_output,
             getattr(baseline, "output_tokens_mean", 0), getattr(baseline, "output_tokens_stddev", 0),
             AnomalyType.OUTPUT_TOKEN_SPIKE),
            ("cost", avg_cost,
             getattr(baseline, "cost_mean", 0), getattr(baseline, "cost_stddev", 0),
             AnomalyType.COST_SPIKE),
            ("retry_rate", retry_rate,
             getattr(baseline, "retry_rate_mean", 0), getattr(baseline, "retry_rate_stddev", 0),
             AnomalyType.HIGH_RETRY_RATE),
            ("error_rate", error_rate,
             getattr(baseline, "error_rate_mean", 0), getattr(baseline, "error_rate_stddev", 0),
             AnomalyType.ERROR_RATE_SPIKE),
        ]

        for name, value, mean, stddev, anomaly_type in checks:
            dev = _safe_deviation(value, mean, stddev)
            if dev is not None:
                deviations[name] = dev
                if dev > self.threshold:
                    anomalies.append(anomaly_type)

        # Prompt hash change detection
        baseline_hash = getattr(baseline, "prompt_hash", "")
        if baseline_hash:
            changed_count = sum(
                1 for v in recent
                if getattr(v, "prompt_hash", "") and getattr(v, "prompt_hash", "") != baseline_hash
            )
            if changed_count >= n // 2 + 1:
                anomalies.append(AnomalyType.PROMPT_CHANGE)
                deviations["prompt_change"] = 10.0

        if anomalies:
            max_dev = max(deviations.values())
            return InfectionReport(
                agent_id=baseline.agent_id,
                max_deviation=max_dev,
                anomalies=anomalies,
                deviations=deviations,
            )

        return None

    def get_anomaly_description(self, anomaly: AnomalyType, baseline, recent_avg: float) -> str:
        descriptions = {
            AnomalyType.TOKEN_SPIKE: f"tokens: {recent_avg:.0f} vs baseline {baseline.tokens_mean:.0f}",
            AnomalyType.LATENCY_SPIKE: f"latency: {recent_avg:.0f}ms vs baseline {baseline.latency_mean:.0f}ms",
            AnomalyType.TOOL_EXPLOSION: f"tool calls: {recent_avg:.1f} vs baseline {baseline.tools_mean:.1f}",
            AnomalyType.HIGH_RETRY_RATE: f"retry rate: {recent_avg:.1%}",
            AnomalyType.INPUT_TOKEN_SPIKE: f"input tokens: {recent_avg:.0f} vs baseline {getattr(baseline, 'input_tokens_mean', 0):.0f}",
            AnomalyType.OUTPUT_TOKEN_SPIKE: f"output tokens: {recent_avg:.0f} vs baseline {getattr(baseline, 'output_tokens_mean', 0):.0f}",
            AnomalyType.COST_SPIKE: f"cost: ${recent_avg:.4f} vs baseline ${getattr(baseline, 'cost_mean', 0):.4f}",
            AnomalyType.PROMPT_CHANGE: "system prompt hash changed",
            AnomalyType.ERROR_RATE_SPIKE: f"error rate: {recent_avg:.1%}",
        }
        return descriptions.get(anomaly, str(anomaly))
