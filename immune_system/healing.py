"""
Healer — Recovery actions, healing policies, and success-weighted selection.

Uses diagnosis type to select an ordered policy of actions.  Skips previously
failed actions via ImmuneMemory.  When global success patterns exist, reorders
the policy to prefer historically successful actions first.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, List, Optional, Set

from .diagnosis import DiagnosisType

if TYPE_CHECKING:
    from .executor import ExecutionResult, HealingExecutor
    from .memory import ImmuneMemory


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
    DiagnosisType.EXTERNAL_CAUSE: [
        HealingAction.REDUCE_AUTONOMY,
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
    """Result of a healing attempt."""
    agent_id: str
    action: HealingAction
    success: bool
    validation_passed: bool
    message: str


class Healer:
    """Applies healing actions to infected agents."""

    def __init__(self, telemetry_collector, baseline_learner, sentinel, executor=None):
        self.telemetry_collector = telemetry_collector
        self.baseline_learner = baseline_learner
        self.sentinel = sentinel
        self.executor = executor
        self.healing_attempts = 0

    def get_healing_policy(self, diagnosis_type: DiagnosisType) -> list:
        return HEALING_POLICIES.get(diagnosis_type, HEALING_POLICIES[DiagnosisType.UNKNOWN])

    def get_next_action(
        self,
        diagnosis_type: DiagnosisType,
        failed_actions: Set[HealingAction],
        immune_memory: Optional[ImmuneMemory] = None,
    ) -> Optional[HealingAction]:
        """Pick the next action, reordering by global success patterns when available."""
        policy = self.get_healing_policy(diagnosis_type)
        candidates = [a for a in policy if a not in failed_actions]

        if not candidates:
            return None

        if immune_memory is not None:
            successful = immune_memory.get_successful_actions(diagnosis_type)
            if successful:
                def _sort_key(action: HealingAction) -> int:
                    try:
                        return successful.index(action)
                    except ValueError:
                        return len(successful)
                candidates.sort(key=_sort_key)

        return candidates[0]

    async def apply_healing(self, agent, action: HealingAction, context: dict = None) -> HealingResult:
        """Apply a healing action using the configured executor or in-memory fallback."""
        self.healing_attempts += 1
        agent_id = agent.agent_id

        if self.executor is not None:
            ctx = context or {}
            ctx.setdefault("agent", agent)
            exec_result = await self.executor.execute(agent_id, action, ctx)
            return HealingResult(
                agent_id=agent_id,
                action=action,
                success=exec_result.success,
                validation_passed=exec_result.success,
                message=exec_result.message,
            )

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
                return HealingResult(agent_id=agent_id, action=action, success=False,
                                     validation_passed=False, message="Unknown healing action")

            agent.cure()
            return HealingResult(agent_id=agent_id, action=action, success=True,
                                 validation_passed=True, message=message)
        except Exception as e:
            return HealingResult(agent_id=agent_id, action=action, success=False,
                                 validation_passed=False, message=f"Healing failed: {e}")

    async def validate_probation(self, agent_id: str) -> bool:
        """Validate healing by checking *fresh* post-healing vitals.

        Called after the agent has been in PROBATION for enough ticks to
        accumulate new telemetry.  Returns True if the agent looks healthy.
        """
        baseline = self.baseline_learner.get_baseline(agent_id)
        if not baseline:
            return True

        recent = self.telemetry_collector.get_recent(agent_id, window_seconds=10)
        if not recent or len(recent) < 3:
            return True

        infection = self.sentinel.detect_infection(recent, baseline)
        return infection is None

    async def _validate_healing(self, agent) -> bool:
        """Legacy validator (checks pre-quarantine vitals).  Deprecated in favor
        of probation-based validation, but kept for backward compat."""
        baseline = self.baseline_learner.get_baseline(agent.agent_id)
        if not baseline:
            return True
        recent = self.telemetry_collector.get_recent(agent.agent_id, window_seconds=5)
        if not recent or len(recent) < 2:
            return True
        infection = self.sentinel.detect_infection(recent, baseline)
        return infection is None
