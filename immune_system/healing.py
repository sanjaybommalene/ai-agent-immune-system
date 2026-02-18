"""
Healer - Recovery actions and healing policies
"""
from dataclasses import dataclass
from enum import Enum
from typing import Optional, Set
from .diagnosis import DiagnosisType
import asyncio


class HealingAction(Enum):
    RESET_MEMORY = "reset_memory"
    ROLLBACK_PROMPT = "rollback_prompt"
    REDUCE_AUTONOMY = "reduce_autonomy"
    REVOKE_TOOLS = "revoke_tools"
    CLONE_AGENT = "clone_agent"


HEALING_POLICIES = {
    DiagnosisType.PROMPT_DRIFT: [
        HealingAction.RESET_MEMORY,
        HealingAction.ROLLBACK_PROMPT,
        HealingAction.REDUCE_AUTONOMY,
        HealingAction.CLONE_AGENT,
    ],
    DiagnosisType.PROMPT_INJECTION: [
        HealingAction.REVOKE_TOOLS,
        HealingAction.RESET_MEMORY,
        HealingAction.ROLLBACK_PROMPT,
        HealingAction.CLONE_AGENT,
    ],
    DiagnosisType.INFINITE_LOOP: [
        HealingAction.REVOKE_TOOLS,
        HealingAction.REDUCE_AUTONOMY,
        HealingAction.RESET_MEMORY,
        HealingAction.CLONE_AGENT,
    ],
    DiagnosisType.TOOL_INSTABILITY: [
        HealingAction.REDUCE_AUTONOMY,
        HealingAction.ROLLBACK_PROMPT,
        HealingAction.CLONE_AGENT,
    ],
    DiagnosisType.MEMORY_CORRUPTION: [
        HealingAction.RESET_MEMORY,
        HealingAction.CLONE_AGENT,
    ],
    DiagnosisType.UNKNOWN: [
        HealingAction.RESET_MEMORY,
        HealingAction.REDUCE_AUTONOMY,
        HealingAction.CLONE_AGENT,
    ],
}


@dataclass
class HealingResult:
    """Result of a healing attempt"""
    agent_id: str
    action: HealingAction
    success: bool
    validation_passed: bool
    message: str


class Healer:
    """Applies healing actions to infected agents"""
    
    def __init__(self, telemetry_collector, baseline_learner, sentinel):
        self.telemetry_collector = telemetry_collector
        self.baseline_learner = baseline_learner
        self.sentinel = sentinel
        self.healing_attempts = 0
    
    def get_healing_policy(self, diagnosis_type: DiagnosisType) -> list:
        """Get ordered healing actions for a diagnosis"""
        return HEALING_POLICIES.get(diagnosis_type, HEALING_POLICIES[DiagnosisType.UNKNOWN])
    
    def get_next_action(self, diagnosis_type: DiagnosisType, failed_actions: Set[HealingAction]) -> Optional[HealingAction]:
        """
        Get next healing action from policy, skipping previously failed actions
        
        Args:
            diagnosis_type: Type of diagnosis
            failed_actions: Set of actions that previously failed for this diagnosis
        
        Returns:
            Next action to try, or None if all exhausted
        """
        policy = self.get_healing_policy(diagnosis_type)
        
        for action in policy:
            if action not in failed_actions:
                return action
        
        return None  # All actions exhausted
    
    async def apply_healing(self, agent, action: HealingAction) -> HealingResult:
        """
        Apply specific healing action to agent
        
        Args:
            agent: The infected agent
            action: Healing action to apply
        
        Returns:
            HealingResult with outcome
        """
        self.healing_attempts += 1
        agent_id = agent.agent_id
        
        try:
            if action == HealingAction.RESET_MEMORY:
                agent.state.reset_memory()
                message = "Memory cleared"
            
            elif action == HealingAction.ROLLBACK_PROMPT:
                agent.state.rollback_prompt()
                message = f"Prompt rolled back to v{agent.state.prompt_version}"
            
            elif action == HealingAction.REDUCE_AUTONOMY:
                agent.state.reduce_autonomy()
                message = f"Autonomy reduced (temp={agent.state.temperature:.2f}, max_tools={agent.state.max_tools})"
            
            elif action == HealingAction.REVOKE_TOOLS:
                agent.state.revoke_tools()
                message = "Tool access revoked"
            
            elif action == HealingAction.CLONE_AGENT:
                # In real system, would create new agent instance
                # For demo, just reset all state
                agent.state = type(agent.state)()
                agent.cure()  # Clear infection
                message = "Agent cloned with clean state"
            
            else:
                return HealingResult(
                    agent_id=agent_id,
                    action=action,
                    success=False,
                    validation_passed=False,
                    message="Unknown healing action"
                )
            
            # Cure the infection (reset infection state)
            agent.cure()
            
            # Wait for a few executions to validate (need more time for behavior to normalize)
            await asyncio.sleep(1.5)
            
            # Validate healing worked
            validation_passed = await self._validate_healing(agent)
            
            return HealingResult(
                agent_id=agent_id,
                action=action,
                success=True,
                validation_passed=validation_passed,
                message=message
            )
        
        except Exception as e:
            return HealingResult(
                agent_id=agent_id,
                action=action,
                success=False,
                validation_passed=False,
                message=f"Healing failed: {str(e)}"
            )
    
    async def _validate_healing(self, agent) -> bool:
        """
        Validate that healing was successful by checking if behavior normalized
        
        Returns:
            True if agent behavior returned to baseline
        """
        # Get baseline
        baseline = self.baseline_learner.get_baseline(agent.agent_id)
        if not baseline:
            return True  # No baseline, assume success
        
        # Get recent telemetry (after healing) - use shorter window
        recent_vitals = self.telemetry_collector.get_recent(agent.agent_id, window_seconds=5)
        if not recent_vitals or len(recent_vitals) < 3:
            return True  # Not enough data yet, assume success (benefit of doubt)
        
        # Check if still showing infection
        infection = self.sentinel.detect_infection(recent_vitals, baseline)
        
        # Success if no infection detected
        return infection is None
