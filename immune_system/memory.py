"""
Immune Memory — Adaptive learning that remembers healing outcomes, learns
which actions work globally, and supports operator feedback.

Key capabilities:
  1. **Negative learning** — never repeat failed actions for the same
     agent + diagnosis.
  2. **Positive learning** — prefer globally successful actions by reordering
     the policy ladder based on cross-agent success patterns.
  3. **Feedback storage** — record operator corrections for diagnosis accuracy.
"""
from __future__ import annotations

import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set

from .diagnosis import DiagnosisFeedback, DiagnosisType
from .healing import HealingAction


@dataclass
class HealingRecord:
    """Record of a healing attempt."""
    agent_id: str
    diagnosis_type: DiagnosisType
    healing_action: HealingAction
    success: bool
    timestamp: float

    def __str__(self):
        icon = "OK" if self.success else "FAIL"
        return f"[{icon}] {self.agent_id}: {self.healing_action.value} for {self.diagnosis_type.value}"


class ImmuneMemory:
    """Remembers healing outcomes and learns which actions work."""

    def __init__(self, store=None):
        self.store = store
        self.records: List[HealingRecord] = []
        self.by_agent_diagnosis: Dict = defaultdict(list)
        self.global_success_patterns: Dict[DiagnosisType, Dict[HealingAction, int]] = defaultdict(lambda: defaultdict(int))
        self.global_failure_patterns: Dict[DiagnosisType, Dict[HealingAction, int]] = defaultdict(lambda: defaultdict(int))
        self._feedback: List[DiagnosisFeedback] = []

    # ── Recording ─────────────────────────────────────────────────────

    def record_healing(self, agent_id: str, diagnosis_type: DiagnosisType,
                       healing_action: HealingAction, success: bool):
        if self.store:
            self.store.write_healing_event(
                agent_id=agent_id,
                diagnosis_type=diagnosis_type.value,
                healing_action=healing_action.value,
                success=success,
                validation_passed=success,
                trigger="memory_record",
                message=None,
            )

        record = HealingRecord(
            agent_id=agent_id,
            diagnosis_type=diagnosis_type,
            healing_action=healing_action,
            success=success,
            timestamp=time.time(),
        )
        self.records.append(record)
        self.by_agent_diagnosis[(agent_id, diagnosis_type)].append(record)

        if success:
            self.global_success_patterns[diagnosis_type][healing_action] += 1
        else:
            self.global_failure_patterns[diagnosis_type][healing_action] += 1

    def record_feedback(self, feedback: DiagnosisFeedback):
        self._feedback.append(feedback)

    # ── Querying ──────────────────────────────────────────────────────

    def get_failed_actions(self, agent_id: str, diagnosis_type: DiagnosisType) -> Set[HealingAction]:
        """Actions that failed for this specific agent + diagnosis."""
        if self.store:
            raw = self.store.get_failed_healing_actions(agent_id, diagnosis_type.value)
            out = set()
            for action in raw:
                try:
                    out.add(HealingAction(action))
                except ValueError:
                    continue
            return out

        records = self.by_agent_diagnosis.get((agent_id, diagnosis_type), [])
        return {r.healing_action for r in records if not r.success}

    def get_successful_actions(self, diagnosis_type: DiagnosisType) -> List[HealingAction]:
        """Actions that worked globally for this diagnosis, sorted by success count."""
        counts = self.global_success_patterns[diagnosis_type]
        sorted_actions = sorted(counts.items(), key=lambda x: x[1], reverse=True)
        return [action for action, _ in sorted_actions]

    def get_success_rate_for_action(self, diagnosis_type: DiagnosisType,
                                     action: HealingAction) -> float:
        """Success rate for a specific action+diagnosis across all agents."""
        s = self.global_success_patterns[diagnosis_type].get(action, 0)
        f = self.global_failure_patterns[diagnosis_type].get(action, 0)
        total = s + f
        return s / total if total > 0 else 0.0

    def get_healing_history(self, agent_id: str) -> List[HealingRecord]:
        return [r for r in self.records if r.agent_id == agent_id]

    def get_total_healings(self) -> int:
        if self.store:
            return self.store.get_total_healings()
        return len(self.records)

    def get_success_rate(self) -> float:
        if self.store:
            return self.store.get_healing_success_rate()
        if not self.records:
            return 0.0
        return sum(1 for r in self.records if r.success) / len(self.records)

    def get_pattern_summary(self) -> Dict:
        if self.store:
            return self.store.get_healing_pattern_summary()
        summary = {}
        for dtype, actions in self.global_success_patterns.items():
            if actions:
                best = max(actions.items(), key=lambda x: x[1])
                summary[dtype.value] = {
                    "best_action": best[0].value,
                    "success_count": best[1],
                }
        return summary

    def has_learning_for(self, agent_id: str, diagnosis_type: DiagnosisType) -> bool:
        if self.store:
            return len(self.store.get_failed_healing_actions(agent_id, diagnosis_type.value)) > 0
        return (agent_id, diagnosis_type) in self.by_agent_diagnosis

    def get_feedback_history(self) -> List[DiagnosisFeedback]:
        return list(self._feedback)
