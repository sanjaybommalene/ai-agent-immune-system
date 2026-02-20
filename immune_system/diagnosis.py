"""
Diagnostician — Multi-hypothesis root-cause analysis.

Instead of returning a single diagnosis, the diagnostician now produces a
**ranked list** of hypotheses sorted by confidence.  The orchestrator tries
healing for the primary hypothesis first; if all actions fail it falls back
to the secondary hypothesis, and so on.

An optional *feedback* history lets the system learn which anomaly patterns
map to which actual root causes over time.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional

from .detection import AnomalyType, InfectionReport


class DiagnosisType(Enum):
    PROMPT_DRIFT = "prompt_drift"
    PROMPT_INJECTION = "prompt_injection"
    INFINITE_LOOP = "infinite_loop"
    TOOL_INSTABILITY = "tool_instability"
    MEMORY_CORRUPTION = "memory_corruption"
    COST_OVERRUN = "cost_overrun"
    EXTERNAL_CAUSE = "external_cause"
    UNKNOWN = "unknown"


@dataclass
class Diagnosis:
    """A single root-cause hypothesis."""
    agent_id: str
    diagnosis_type: DiagnosisType
    confidence: float
    reasoning: str

    def __str__(self):
        return f"Diagnosis[{self.agent_id}]: {self.diagnosis_type.value} (confidence={self.confidence:.0%})"


@dataclass
class DiagnosisContext:
    """Fleet-wide and environmental context available at diagnosis time."""
    fleet_wide: bool = False
    affected_fraction: float = 0.0
    affected_agents: List[str] = field(default_factory=list)
    correlation_detail: str = ""


@dataclass
class DiagnosisResult:
    """Multi-hypothesis diagnosis with context."""
    agent_id: str
    hypotheses: List[Diagnosis]
    context: DiagnosisContext = field(default_factory=DiagnosisContext)

    @property
    def primary(self) -> Diagnosis:
        return self.hypotheses[0]

    def __str__(self):
        hyps = ", ".join(f"{h.diagnosis_type.value}({h.confidence:.0%})" for h in self.hypotheses)
        return f"DiagnosisResult[{self.agent_id}]: [{hyps}]"


@dataclass
class DiagnosisFeedback:
    """Operator feedback on a past diagnosis."""
    agent_id: str
    original_type: DiagnosisType
    actual_cause: str
    notes: str = ""
    timestamp: float = 0.0


class Diagnostician:
    """Multi-hypothesis diagnostician with operator feedback learning."""

    def __init__(self):
        self._feedback_history: List[DiagnosisFeedback] = []
        self._confidence_adjustments: Dict[DiagnosisType, float] = {}

    def record_feedback(self, feedback: DiagnosisFeedback):
        self._feedback_history.append(feedback)
        if feedback.actual_cause == "false_positive":
            adj = self._confidence_adjustments.get(feedback.original_type, 0.0)
            self._confidence_adjustments[feedback.original_type] = adj - 0.05
        elif feedback.actual_cause == "wrong_diagnosis":
            adj = self._confidence_adjustments.get(feedback.original_type, 0.0)
            self._confidence_adjustments[feedback.original_type] = adj - 0.03

    def _adjust(self, dtype: DiagnosisType, base_confidence: float) -> float:
        adj = self._confidence_adjustments.get(dtype, 0.0)
        return max(0.05, min(1.0, base_confidence + adj))

    def diagnose(self, infection: InfectionReport, baseline,
                 context: Optional[DiagnosisContext] = None) -> DiagnosisResult:
        """Return a multi-hypothesis diagnosis sorted by confidence."""
        agent_id = infection.agent_id
        anomalies = set(infection.anomalies)
        devs = infection.deviations
        ctx = context or DiagnosisContext()

        hypotheses: List[Diagnosis] = []

        if ctx.fleet_wide:
            hypotheses.append(Diagnosis(
                agent_id=agent_id,
                diagnosis_type=DiagnosisType.EXTERNAL_CAUSE,
                confidence=self._adjust(DiagnosisType.EXTERNAL_CAUSE, 0.90),
                reasoning=(
                    f"Fleet-wide anomaly detected ({ctx.affected_fraction:.0%} of fleet affected). "
                    f"{ctx.correlation_detail}"
                ),
            ))

        if AnomalyType.PROMPT_CHANGE in anomalies:
            conf = 0.95 if AnomalyType.INPUT_TOKEN_SPIKE in anomalies else 0.80
            hypotheses.append(Diagnosis(
                agent_id=agent_id,
                diagnosis_type=DiagnosisType.PROMPT_INJECTION,
                confidence=self._adjust(DiagnosisType.PROMPT_INJECTION, conf),
                reasoning="Prompt hash changed" + (
                    " with input token spike — likely prompt injection" if AnomalyType.INPUT_TOKEN_SPIKE in anomalies
                    else " unexpectedly — possible prompt manipulation"
                ),
            ))
            hypotheses.append(Diagnosis(
                agent_id=agent_id,
                diagnosis_type=DiagnosisType.PROMPT_DRIFT,
                confidence=self._adjust(DiagnosisType.PROMPT_DRIFT, conf * 0.6),
                reasoning="Prompt change could also be intentional drift or operator update",
            ))

        if AnomalyType.INPUT_TOKEN_SPIKE in anomalies and devs.get("input_tokens", 0) > 3.0:
            if not any(h.diagnosis_type == DiagnosisType.PROMPT_INJECTION for h in hypotheses):
                hypotheses.append(Diagnosis(
                    agent_id=agent_id,
                    diagnosis_type=DiagnosisType.PROMPT_INJECTION,
                    confidence=self._adjust(DiagnosisType.PROMPT_INJECTION, 0.85),
                    reasoning="Input token spike >3σ suggests context stuffing",
                ))

        if AnomalyType.OUTPUT_TOKEN_SPIKE in anomalies and devs.get("output_tokens", 0) > 3.0:
            hypotheses.append(Diagnosis(
                agent_id=agent_id,
                diagnosis_type=DiagnosisType.PROMPT_DRIFT,
                confidence=self._adjust(DiagnosisType.PROMPT_DRIFT, 0.85),
                reasoning="Output token explosion >3σ indicates runaway generation",
            ))

        if AnomalyType.TOKEN_SPIKE in anomalies and devs.get("tokens", 0) > 3.0:
            if not any(h.diagnosis_type == DiagnosisType.PROMPT_DRIFT for h in hypotheses):
                hypotheses.append(Diagnosis(
                    agent_id=agent_id,
                    diagnosis_type=DiagnosisType.PROMPT_DRIFT,
                    confidence=self._adjust(DiagnosisType.PROMPT_DRIFT, 0.85),
                    reasoning="Token usage spike >3σ suggests prompt drift",
                ))

        if AnomalyType.COST_SPIKE in anomalies:
            hypotheses.append(Diagnosis(
                agent_id=agent_id,
                diagnosis_type=DiagnosisType.COST_OVERRUN,
                confidence=self._adjust(DiagnosisType.COST_OVERRUN, 0.80),
                reasoning=f"Cost deviation ({devs.get('cost', 0):.1f}σ) exceeds threshold",
            ))

        if AnomalyType.TOOL_EXPLOSION in anomalies and devs.get("tools", 0) > 3.0:
            hypotheses.append(Diagnosis(
                agent_id=agent_id,
                diagnosis_type=DiagnosisType.INFINITE_LOOP,
                confidence=self._adjust(DiagnosisType.INFINITE_LOOP, 0.90),
                reasoning="Excessive tool calls indicate potential infinite loop",
            ))

        if AnomalyType.LATENCY_SPIKE in anomalies or AnomalyType.ERROR_RATE_SPIKE in anomalies:
            lat = AnomalyType.LATENCY_SPIKE in anomalies
            err = AnomalyType.ERROR_RATE_SPIKE in anomalies
            retry = AnomalyType.HIGH_RETRY_RATE in anomalies
            conf = 0.75 if (lat and (err or retry)) else (0.70 if err else 0.60)
            hypotheses.append(Diagnosis(
                agent_id=agent_id,
                diagnosis_type=DiagnosisType.TOOL_INSTABILITY,
                confidence=self._adjust(DiagnosisType.TOOL_INSTABILITY, conf),
                reasoning="Latency/error/retry pattern suggests tool or provider instability",
            ))

        if AnomalyType.HIGH_RETRY_RATE in anomalies:
            if not any(h.diagnosis_type == DiagnosisType.TOOL_INSTABILITY for h in hypotheses):
                hypotheses.append(Diagnosis(
                    agent_id=agent_id,
                    diagnosis_type=DiagnosisType.MEMORY_CORRUPTION,
                    confidence=self._adjust(DiagnosisType.MEMORY_CORRUPTION, 0.65),
                    reasoning="High retry rate may indicate corrupted agent state",
                ))

        if not hypotheses:
            hypotheses.append(Diagnosis(
                agent_id=agent_id,
                diagnosis_type=DiagnosisType.UNKNOWN,
                confidence=0.30,
                reasoning="Anomaly pattern does not match known failure modes",
            ))

        hypotheses.sort(key=lambda h: h.confidence, reverse=True)

        seen = set()
        deduped = []
        for h in hypotheses:
            if h.diagnosis_type not in seen:
                seen.add(h.diagnosis_type)
                deduped.append(h)

        return DiagnosisResult(agent_id=agent_id, hypotheses=deduped, context=ctx)

    def diagnose_single(self, infection: InfectionReport, baseline) -> Diagnosis:
        """Backward-compatible single-diagnosis interface."""
        return self.diagnose(infection, baseline).primary
