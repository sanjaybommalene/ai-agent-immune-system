"""
Diagnostician - Root cause analysis of infections
"""
from dataclasses import dataclass
from enum import Enum
from detection import AnomalyType, InfectionReport


class DiagnosisType(Enum):
    PROMPT_DRIFT = "prompt_drift"
    INFINITE_LOOP = "infinite_loop"
    TOOL_INSTABILITY = "tool_instability"
    MEMORY_CORRUPTION = "memory_corruption"
    UNKNOWN = "unknown"


@dataclass
class Diagnosis:
    """Root cause diagnosis"""
    agent_id: str
    diagnosis_type: DiagnosisType
    confidence: float  # 0-1
    reasoning: str
    
    def __str__(self):
        return f"Diagnosis[{self.agent_id}]: {self.diagnosis_type.value} (confidence={self.confidence:.0%})"


class Diagnostician:
    """Diagnoses root cause of agent infections"""
    
    def diagnose(self, infection: InfectionReport, baseline) -> Diagnosis:
        """
        Determine likely root cause based on anomaly patterns
        
        Args:
            infection: InfectionReport from Sentinel
            baseline: Agent's baseline profile
        
        Returns:
            Diagnosis with root cause and confidence
        """
        agent_id = infection.agent_id
        anomalies = infection.anomalies
        
        # Rule-based diagnosis logic
        
        # Pattern 1: Token spike suggests prompt drift
        if AnomalyType.TOKEN_SPIKE in anomalies:
            if infection.deviations.get('tokens', 0) > 3.0:
                return Diagnosis(
                    agent_id=agent_id,
                    diagnosis_type=DiagnosisType.PROMPT_DRIFT,
                    confidence=0.85,
                    reasoning="Significant token usage increase (>3σ) suggests prompt drift or response inflation"
                )
        
        # Pattern 2: Tool explosion suggests infinite loop
        if AnomalyType.TOOL_EXPLOSION in anomalies:
            if infection.deviations.get('tools', 0) > 3.0:
                return Diagnosis(
                    agent_id=agent_id,
                    diagnosis_type=DiagnosisType.INFINITE_LOOP,
                    confidence=0.90,
                    reasoning="Excessive tool calls indicate potential infinite loop or recursion"
                )
        
        # Pattern 3: Latency spike + high retry suggests tool instability
        if AnomalyType.LATENCY_SPIKE in anomalies and AnomalyType.HIGH_RETRY_RATE in anomalies:
            return Diagnosis(
                agent_id=agent_id,
                diagnosis_type=DiagnosisType.TOOL_INSTABILITY,
                confidence=0.75,
                reasoning="Latency spike with retries suggests external tool instability"
            )
        
        # Pattern 4: Just latency spike might be tool instability
        if AnomalyType.LATENCY_SPIKE in anomalies:
            return Diagnosis(
                agent_id=agent_id,
                diagnosis_type=DiagnosisType.TOOL_INSTABILITY,
                confidence=0.60,
                reasoning="Isolated latency spike may indicate tool performance issues"
            )
        
        # Pattern 5: High retry rate might be memory corruption
        if AnomalyType.HIGH_RETRY_RATE in anomalies:
            return Diagnosis(
                agent_id=agent_id,
                diagnosis_type=DiagnosisType.MEMORY_CORRUPTION,
                confidence=0.65,
                reasoning="High retry rate may indicate corrupted agent state"
            )
        
        # Default: Unknown
        return Diagnosis(
            agent_id=agent_id,
            diagnosis_type=DiagnosisType.UNKNOWN,
            confidence=0.30,
            reasoning="Anomaly pattern does not match known failure modes"
        )
