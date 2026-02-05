"""
Sentinel - Anomaly detection system
"""
from dataclasses import dataclass
from typing import List, Optional
from enum import Enum


class AnomalyType(Enum):
    TOKEN_SPIKE = "token_spike"
    LATENCY_SPIKE = "latency_spike"
    TOOL_EXPLOSION = "tool_explosion"
    HIGH_RETRY_RATE = "high_retry_rate"


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
        Detect if agent is infected by comparing recent behavior to baseline
        
        Args:
            recent_vitals: Recent telemetry data
            baseline: BaselineProfile for this agent
        
        Returns:
            InfectionReport if infection detected, None otherwise
        """
        if not recent_vitals or not baseline:
            return None
        
        # Get most recent vitals (last 3-5 executions)
        sample_size = min(5, len(recent_vitals))
        recent_sample = recent_vitals[-sample_size:]
        
        # Calculate average of recent behavior
        avg_latency = sum(v.latency_ms for v in recent_sample) / len(recent_sample)
        avg_tokens = sum(v.token_count for v in recent_sample) / len(recent_sample)
        avg_tools = sum(v.tool_calls for v in recent_sample) / len(recent_sample)
        
        # Calculate deviations (in standard deviations)
        deviations = {}
        anomalies = []
        
        # Latency deviation
        if baseline.latency_stddev > 0:
            latency_dev = abs(avg_latency - baseline.latency_mean) / baseline.latency_stddev
            deviations['latency'] = latency_dev
            if latency_dev > self.threshold:
                anomalies.append(AnomalyType.LATENCY_SPIKE)
        
        # Token deviation
        if baseline.tokens_stddev > 0:
            tokens_dev = abs(avg_tokens - baseline.tokens_mean) / baseline.tokens_stddev
            deviations['tokens'] = tokens_dev
            if tokens_dev > self.threshold:
                anomalies.append(AnomalyType.TOKEN_SPIKE)
        
        # Tool calls deviation
        if baseline.tools_stddev > 0:
            tools_dev = abs(avg_tools - baseline.tools_mean) / baseline.tools_stddev
            deviations['tools'] = tools_dev
            if tools_dev > self.threshold:
                anomalies.append(AnomalyType.TOOL_EXPLOSION)
        
        # Check retry rate
        retry_rate = sum(1 for v in recent_sample if v.retries > 0) / len(recent_sample)
        if retry_rate > 0.3:  # More than 30% retries
            anomalies.append(AnomalyType.HIGH_RETRY_RATE)
            deviations['retry_rate'] = retry_rate
        
        # If any anomalies detected, create infection report
        if anomalies:
            # Calculate severity (max deviation, capped at 10)
            severity = min(10.0, max(deviations.values()))
            
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
        return str(anomaly)
