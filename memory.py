"""
Immune Memory - Learning system that remembers which healing actions work
"""
from dataclasses import dataclass
from typing import List, Set, Dict
from collections import defaultdict
from diagnosis import DiagnosisType
from healing import HealingAction
import time


@dataclass
class HealingRecord:
    """Record of a healing attempt"""
    agent_id: str
    diagnosis_type: DiagnosisType
    healing_action: HealingAction
    success: bool
    timestamp: float
    
    def __str__(self):
        status = "✅" if self.success else "❌"
        return f"{status} {self.agent_id}: {self.healing_action.value} for {self.diagnosis_type.value}"


class ImmuneMemory:
    """
    Remembers healing outcomes and learns which actions work
    
    Key principle: NEVER repeat failed actions for the same diagnosis
    """
    
    def __init__(self):
        # Store all healing records
        self.records: List[HealingRecord] = []
        
        # Index: (agent_id, diagnosis_type) -> [HealingRecord]
        self.by_agent_diagnosis: Dict = defaultdict(list)
        
        # Global learning: diagnosis_type -> successful_actions
        self.global_success_patterns: Dict[DiagnosisType, Dict[HealingAction, int]] = defaultdict(lambda: defaultdict(int))
    
    def record_healing(self, agent_id: str, diagnosis_type: DiagnosisType, 
                      healing_action: HealingAction, success: bool):
        """
        Record outcome of a healing attempt
        
        Args:
            agent_id: Agent that was healed
            diagnosis_type: Diagnosis that was treated
            healing_action: Action that was attempted
            success: Whether healing succeeded
        """
        record = HealingRecord(
            agent_id=agent_id,
            diagnosis_type=diagnosis_type,
            healing_action=healing_action,
            success=success,
            timestamp=time.time()
        )
        
        self.records.append(record)
        self.by_agent_diagnosis[(agent_id, diagnosis_type)].append(record)
        
        # Update global learning patterns
        if success:
            self.global_success_patterns[diagnosis_type][healing_action] += 1
    
    def get_failed_actions(self, agent_id: str, diagnosis_type: DiagnosisType) -> Set[HealingAction]:
        """
        Get set of healing actions that previously FAILED for this agent + diagnosis
        
        This is the core of adaptive immunity: we never repeat failed cures
        
        Args:
            agent_id: Agent ID
            diagnosis_type: Diagnosis type
        
        Returns:
            Set of HealingActions that failed before
        """
        records = self.by_agent_diagnosis.get((agent_id, diagnosis_type), [])
        
        failed_actions = set()
        for record in records:
            if not record.success:
                failed_actions.add(record.healing_action)
        
        return failed_actions
    
    def get_successful_actions(self, diagnosis_type: DiagnosisType) -> List[HealingAction]:
        """
        Get healing actions that worked globally for this diagnosis type,
        sorted by success rate
        
        Args:
            diagnosis_type: Diagnosis type
        
        Returns:
            List of HealingActions sorted by success count
        """
        success_counts = self.global_success_patterns[diagnosis_type]
        
        # Sort by success count
        sorted_actions = sorted(success_counts.items(), key=lambda x: x[1], reverse=True)
        
        return [action for action, count in sorted_actions]
    
    def get_healing_history(self, agent_id: str) -> List[HealingRecord]:
        """Get all healing records for an agent"""
        return [r for r in self.records if r.agent_id == agent_id]
    
    def get_total_healings(self) -> int:
        """Get total number of healing attempts"""
        return len(self.records)
    
    def get_success_rate(self) -> float:
        """Get overall healing success rate"""
        if not self.records:
            return 0.0
        
        successes = sum(1 for r in self.records if r.success)
        return successes / len(self.records)
    
    def get_pattern_summary(self) -> Dict:
        """Get summary of learned patterns"""
        summary = {}
        
        for diagnosis_type, actions in self.global_success_patterns.items():
            if actions:
                best_action = max(actions.items(), key=lambda x: x[1])
                summary[diagnosis_type.value] = {
                    'best_action': best_action[0].value,
                    'success_count': best_action[1]
                }
        
        return summary
    
    def has_learning_for(self, agent_id: str, diagnosis_type: DiagnosisType) -> bool:
        """Check if we have any learning for this agent + diagnosis combination"""
        return (agent_id, diagnosis_type) in self.by_agent_diagnosis
