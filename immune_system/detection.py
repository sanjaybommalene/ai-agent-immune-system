"""
Sentinel - Anomaly detection system.

Deviation is calculated HERE: detect_infection(recent_vitals, baseline) compares
recent vitals to the agent's baseline (mean/stddev per metric) and produces
per-metric deviations and an overall severity (0-10). Both recent_vitals and
baseline can be backed by InfluxDB (or server API store): recent from
get_recent_agent_vitals(), baseline from get_baseline_profile() (baseline is
learned from older metric data in the store). See docs/DOCS.md §4.
"""
from dataclasses import dataclass
from typing import List, Optional
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


@dataclass
class InfectionReport:
    """Report of detected infection"""
    agent_id: str
    severity: float  # 0-10 scale
    anomalies: List[AnomalyType]
    deviations: dict  # Metric -> deviation score
    
    def __str__(self):
        anomaly_str = ", ".join([a.value for a in self.anomalies])
        return f"InfectionReport[{self.agent_id}]: severity={self.severity:.1f}, anomalies=[{anomaly_str}]"


class Sentinel:
    """Detects abnormal agent behavior"""
    
    def __init__(self, threshold_stddev: float = 2.5):
        self.threshold = threshold_stddev
    
    def detect_infection(self, recent_vitals: List, baseline) -> Optional[InfectionReport]:
        """
        Detect if agent is infected by comparing recent behavior to baseline.

        Args:
            recent_vitals: Recent telemetry data (e.g. from store.get_recent_agent_vitals)
            baseline: BaselineProfile for this agent (e.g. from store.get_baseline_profile)

        Returns:
            InfectionReport if infection detected (with severity 0-10), None otherwise.
        """
        if not recent_vitals or not baseline:
            return None
        
        sample_size = min(5, len(recent_vitals))
        recent_sample = recent_vitals[-sample_size:]
        
        avg_latency = sum(v.latency_ms for v in recent_sample) / len(recent_sample)
        avg_tokens = sum(v.token_count for v in recent_sample) / len(recent_sample)
        avg_tools = sum(v.tool_calls for v in recent_sample) / len(recent_sample)
        avg_input_tokens = sum(getattr(v, 'input_tokens', 0) for v in recent_sample) / len(recent_sample)
        avg_output_tokens = sum(getattr(v, 'output_tokens', 0) for v in recent_sample) / len(recent_sample)
        avg_cost = sum(getattr(v, 'cost', 0.0) for v in recent_sample) / len(recent_sample)
        
        deviations = {}
        anomalies = []
        
        # Latency deviation
        if baseline.latency_stddev > 0:
            latency_dev = abs(avg_latency - baseline.latency_mean) / baseline.latency_stddev
            deviations['latency'] = latency_dev
            if latency_dev > self.threshold:
                anomalies.append(AnomalyType.LATENCY_SPIKE)
        
        # Total token deviation (backward compat)
        if baseline.tokens_stddev > 0:
            tokens_dev = abs(avg_tokens - baseline.tokens_mean) / baseline.tokens_stddev
            deviations['tokens'] = tokens_dev
            if tokens_dev > self.threshold:
                anomalies.append(AnomalyType.TOKEN_SPIKE)

        # Input token deviation (context stuffing / prompt injection signal)
        input_stddev = getattr(baseline, 'input_tokens_stddev', 0)
        input_mean = getattr(baseline, 'input_tokens_mean', 0)
        if input_stddev > 0:
            input_dev = abs(avg_input_tokens - input_mean) / input_stddev
            deviations['input_tokens'] = input_dev
            if input_dev > self.threshold:
                anomalies.append(AnomalyType.INPUT_TOKEN_SPIKE)

        # Output token deviation (runaway generation signal)
        output_stddev = getattr(baseline, 'output_tokens_stddev', 0)
        output_mean = getattr(baseline, 'output_tokens_mean', 0)
        if output_stddev > 0:
            output_dev = abs(avg_output_tokens - output_mean) / output_stddev
            deviations['output_tokens'] = output_dev
            if output_dev > self.threshold:
                anomalies.append(AnomalyType.OUTPUT_TOKEN_SPIKE)

        # Cost deviation
        cost_stddev = getattr(baseline, 'cost_stddev', 0)
        cost_mean = getattr(baseline, 'cost_mean', 0)
        if cost_stddev > 0:
            cost_dev = abs(avg_cost - cost_mean) / cost_stddev
            deviations['cost'] = cost_dev
            if cost_dev > self.threshold:
                anomalies.append(AnomalyType.COST_SPIKE)
        
        # Tool calls deviation
        if baseline.tools_stddev > 0:
            tools_dev = abs(avg_tools - baseline.tools_mean) / baseline.tools_stddev
            deviations['tools'] = tools_dev
            if tools_dev > self.threshold:
                anomalies.append(AnomalyType.TOOL_EXPLOSION)
        
        # Retry rate
        retry_rate = sum(1 for v in recent_sample if v.retries > 0) / len(recent_sample)
        if retry_rate > 0.3:
            anomalies.append(AnomalyType.HIGH_RETRY_RATE)
            deviations['retry_rate'] = retry_rate * 10.0

        # Error rate
        error_rate = sum(1 for v in recent_sample if getattr(v, 'error_type', '')) / len(recent_sample)
        if error_rate > 0.3:
            anomalies.append(AnomalyType.ERROR_RATE_SPIKE)
            deviations['error_rate'] = error_rate * 10.0

        # Prompt hash change detection
        baseline_hash = getattr(baseline, 'prompt_hash', '')
        if baseline_hash:
            changed_count = sum(
                1 for v in recent_sample
                if getattr(v, 'prompt_hash', '') and getattr(v, 'prompt_hash', '') != baseline_hash
            )
            if changed_count >= len(recent_sample) // 2 + 1:
                anomalies.append(AnomalyType.PROMPT_CHANGE)
                deviations['prompt_change'] = 10.0
        
        if anomalies:
            max_dev = max(deviations.values())
            severity = min(10.0, round(2.0 + max_dev * 0.45, 1))
            
            return InfectionReport(
                agent_id=baseline.agent_id,
                severity=severity,
                anomalies=anomalies,
                deviations=deviations
            )
        
        return None
    
    def get_anomaly_description(self, anomaly: AnomalyType, baseline, recent_avg: float) -> str:
        """Get human-readable description of anomaly"""
        if anomaly == AnomalyType.TOKEN_SPIKE:
            return f"tokens: {recent_avg:.0f} vs baseline {baseline.tokens_mean:.0f}"
        elif anomaly == AnomalyType.LATENCY_SPIKE:
            return f"latency: {recent_avg:.0f}ms vs baseline {baseline.latency_mean:.0f}ms"
        elif anomaly == AnomalyType.TOOL_EXPLOSION:
            return f"tool calls: {recent_avg:.1f} vs baseline {baseline.tools_mean:.1f}"
        elif anomaly == AnomalyType.HIGH_RETRY_RATE:
            return f"retry rate: {recent_avg:.1%}"
        elif anomaly == AnomalyType.INPUT_TOKEN_SPIKE:
            return f"input tokens: {recent_avg:.0f} vs baseline {getattr(baseline, 'input_tokens_mean', 0):.0f}"
        elif anomaly == AnomalyType.OUTPUT_TOKEN_SPIKE:
            return f"output tokens: {recent_avg:.0f} vs baseline {getattr(baseline, 'output_tokens_mean', 0):.0f}"
        elif anomaly == AnomalyType.COST_SPIKE:
            return f"cost: ${recent_avg:.4f} vs baseline ${getattr(baseline, 'cost_mean', 0):.4f}"
        elif anomaly == AnomalyType.PROMPT_CHANGE:
            return "system prompt hash changed"
        elif anomaly == AnomalyType.ERROR_RATE_SPIKE:
            return f"error rate: {recent_avg:.1%}"
        return str(anomaly)
