"""
Healer — Recovery actions and healing policies.

Uses diagnosis type to select an ordered policy of actions.  Skips previously
failed actions via ImmuneMemory.  Validates healing by re-running Sentinel.
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
    RESET_AGENT = "reset_agent"


HEALING_POLICIES = {
    DiagnosisType.PROMPT_DRIFT: [
        HealingAction.RESET_MEMORY,
        HealingAction.ROLLBACK_PROMPT,
        HealingAction.REDUCE_AUTONOMY,
        HealingAction.RESET_AGENT,
    ],
    DiagnosisType.PROMPT_INJECTION: [
        HealingAction.REVOKE_TOOLS,
        HealingAction.RESET_MEMORY,
        HealingAction.ROLLBACK_PROMPT,
        HealingAction.RESET_AGENT,
    ],
    DiagnosisType.INFINITE_LOOP: [
        HealingAction.REVOKE_TOOLS,
        HealingAction.REDUCE_AUTONOMY,
        HealingAction.RESET_MEMORY,
        HealingAction.RESET_AGENT,
    ],
    DiagnosisType.TOOL_INSTABILITY: [
        HealingAction.REDUCE_AUTONOMY,
        HealingAction.ROLLBACK_PROMPT,
        HealingAction.RESET_AGENT,
    ],
    DiagnosisType.MEMORY_CORRUPTION: [
        HealingAction.RESET_MEMORY,
        HealingAction.RESET_AGENT,
    ],
    DiagnosisType.COST_OVERRUN: [
        HealingAction.REDUCE_AUTONOMY,
        HealingAction.ROLLBACK_PROMPT,
        HealingAction.RESET_MEMORY,
        HealingAction.RESET_AGENT,
    ],
    DiagnosisType.UNKNOWN: [
        HealingAction.RESET_MEMORY,
        HealingAction.REDUCE_AUTONOMY,
        HealingAction.RESET_AGENT,
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
        return HEALING_POLICIES.get(diagnosis_type, HEALING_POLICIES[DiagnosisType.UNKNOWN])

    def get_next_action(self, diagnosis_type: DiagnosisType, failed_actions: Set[HealingAction]) -> Optional[HealingAction]:
        policy = self.get_healing_policy(diagnosis_type)
        for action in policy:
            if action not in failed_actions:
                return action
        return None

    async def apply_healing(self, agent, action: HealingAction) -> HealingResult:
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
            elif action == HealingAction.RESET_AGENT:
                agent.state = type(agent.state)()
                agent.cure()
                message = "Agent reset to clean state"
            else:
                return HealingResult(
                    agent_id=agent_id, action=action,
                    success=False, validation_passed=False,
                    message="Unknown healing action",
                )

            agent.cure()
            await asyncio.sleep(1.5)
            validation_passed = await self._validate_healing(agent)

            return HealingResult(
                agent_id=agent_id, action=action,
                success=True, validation_passed=validation_passed,
                message=message,
            )
        except Exception as e:
            return HealingResult(
                agent_id=agent_id, action=action,
                success=False, validation_passed=False,
                message=f"Healing failed: {str(e)}",
            )

    async def _validate_healing(self, agent) -> bool:
        baseline = self.baseline_learner.get_baseline(agent.agent_id)
        if not baseline:
            return True

        recent_vitals = self.telemetry_collector.get_recent(agent.agent_id, window_seconds=5)
        if not recent_vitals or len(recent_vitals) < 2:
            return True

        infection = self.sentinel.detect_infection(recent_vitals, baseline)
        return infection is None
