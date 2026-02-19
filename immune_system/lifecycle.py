"""
Agent Lifecycle — Formal state machine governing agent status transitions.

States
------
INITIALIZING  Registered, learning baseline.  No detection runs.
HEALTHY       Normal operation.  Full access.
SUSPECTED     Anomaly detected, under observation for ``suspect_ticks``.
DRAINING      Quarantine ordered.  New requests blocked; in-flight may finish.
QUARANTINED   Fully isolated.  All execution blocked.
HEALING       Active healing in progress.  Execution blocked.
PROBATION     Healed, under observation.  Execution allowed; fresh vitals collected.
EXHAUSTED     All healing actions failed.  Execution blocked; manual intervention required.

Every transition is guarded and logged so the full history can be reconstructed.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Dict, List, Optional

from .logging_config import get_logger

logger = get_logger("lifecycle")


class AgentPhase(Enum):
    INITIALIZING = "initializing"
    HEALTHY = "healthy"
    SUSPECTED = "suspected"
    DRAINING = "draining"
    QUARANTINED = "quarantined"
    HEALING = "healing"
    PROBATION = "probation"
    EXHAUSTED = "exhausted"


_ALLOWED_TRANSITIONS: Dict[AgentPhase, List[AgentPhase]] = {
    AgentPhase.INITIALIZING: [AgentPhase.HEALTHY],
    AgentPhase.HEALTHY: [AgentPhase.SUSPECTED, AgentPhase.DRAINING],
    AgentPhase.SUSPECTED: [AgentPhase.HEALTHY, AgentPhase.DRAINING],
    AgentPhase.DRAINING: [AgentPhase.QUARANTINED],
    AgentPhase.QUARANTINED: [AgentPhase.HEALING],
    AgentPhase.HEALING: [AgentPhase.PROBATION, AgentPhase.EXHAUSTED],
    AgentPhase.PROBATION: [AgentPhase.HEALTHY, AgentPhase.HEALING],
    AgentPhase.EXHAUSTED: [AgentPhase.HEALING],
}


@dataclass
class TransitionEvent:
    """Immutable record of a lifecycle transition."""
    agent_id: str
    from_phase: AgentPhase
    to_phase: AgentPhase
    reason: str
    timestamp: float = field(default_factory=time.time)

    def __str__(self) -> str:
        return (
            f"[{self.agent_id}] {self.from_phase.value} -> {self.to_phase.value}"
            f" ({self.reason})"
        )


SUSPECT_TICKS_DEFAULT = 3
DRAIN_TIMEOUT_DEFAULT = 30.0
PROBATION_TICKS_DEFAULT = 10


@dataclass
class _AgentLifecycleState:
    phase: AgentPhase = AgentPhase.INITIALIZING
    suspect_tick_count: int = 0
    drain_started_at: Optional[float] = None
    probation_tick_count: int = 0
    last_transition_at: float = field(default_factory=time.time)


class LifecycleManager:
    """Manages lifecycle phase for every agent.

    Parameters
    ----------
    suspect_ticks : int
        Number of consecutive anomaly detections required before escalating
        from SUSPECTED to DRAINING.  A single-tick anomaly that resolves on
        the next tick returns the agent to HEALTHY.
    drain_timeout_s : float
        Maximum seconds to stay in DRAINING before forcing QUARANTINED.
    probation_ticks : int
        Number of ticks an agent runs in PROBATION after healing before the
        system decides whether the heal succeeded.
    on_transition : callable, optional
        Callback invoked after every successful transition with a
        ``TransitionEvent``.
    """

    def __init__(
        self,
        suspect_ticks: int = SUSPECT_TICKS_DEFAULT,
        drain_timeout_s: float = DRAIN_TIMEOUT_DEFAULT,
        probation_ticks: int = PROBATION_TICKS_DEFAULT,
        on_transition: Optional[Callable[[TransitionEvent], None]] = None,
    ):
        self.suspect_ticks = suspect_ticks
        self.drain_timeout_s = drain_timeout_s
        self.probation_ticks = probation_ticks
        self.on_transition = on_transition
        self._states: Dict[str, _AgentLifecycleState] = {}
        self._history: List[TransitionEvent] = []

    def _state(self, agent_id: str) -> _AgentLifecycleState:
        if agent_id not in self._states:
            self._states[agent_id] = _AgentLifecycleState()
        return self._states[agent_id]

    def get_phase(self, agent_id: str) -> AgentPhase:
        return self._state(agent_id).phase

    def transition(self, agent_id: str, target: AgentPhase, reason: str) -> bool:
        """Attempt a transition.  Returns True on success, False if disallowed."""
        st = self._state(agent_id)
        if target not in _ALLOWED_TRANSITIONS.get(st.phase, []):
            logger.warning(
                "Blocked transition %s -> %s for %s (%s)",
                st.phase.value, target.value, agent_id, reason,
            )
            return False

        event = TransitionEvent(
            agent_id=agent_id,
            from_phase=st.phase,
            to_phase=target,
            reason=reason,
        )

        old_phase = st.phase
        st.phase = target
        st.last_transition_at = event.timestamp

        if target == AgentPhase.SUSPECTED:
            st.suspect_tick_count = 1
        elif target == AgentPhase.DRAINING:
            st.drain_started_at = event.timestamp
        elif target == AgentPhase.PROBATION:
            st.probation_tick_count = 0

        self._history.append(event)
        logger.info("Lifecycle: %s", event)

        if self.on_transition:
            self.on_transition(event)
        return True

    # ── Convenience helpers used by the orchestrator ──────────────────

    def mark_baseline_ready(self, agent_id: str) -> bool:
        return self.transition(agent_id, AgentPhase.HEALTHY, "baseline_ready")

    def record_anomaly_tick(self, agent_id: str) -> AgentPhase:
        """Called each tick an anomaly is detected while SUSPECTED.

        Returns the current phase after the call (may remain SUSPECTED or
        escalate to DRAINING).
        """
        st = self._state(agent_id)
        if st.phase == AgentPhase.HEALTHY:
            self.transition(agent_id, AgentPhase.SUSPECTED, "anomaly_detected")
            return self.get_phase(agent_id)

        if st.phase == AgentPhase.SUSPECTED:
            st.suspect_tick_count += 1
            if st.suspect_tick_count >= self.suspect_ticks:
                self.transition(agent_id, AgentPhase.DRAINING, "anomaly_persisted")
            return self.get_phase(agent_id)

        return st.phase

    def record_anomaly_resolved(self, agent_id: str) -> bool:
        """Called when a SUSPECTED agent shows no anomaly on a tick."""
        st = self._state(agent_id)
        if st.phase == AgentPhase.SUSPECTED:
            return self.transition(agent_id, AgentPhase.HEALTHY, "anomaly_resolved")
        return False

    def force_drain(self, agent_id: str, reason: str = "severe_anomaly") -> bool:
        """Skip SUSPECTED and go straight to DRAINING (for high-deviation)."""
        st = self._state(agent_id)
        if st.phase in (AgentPhase.HEALTHY, AgentPhase.SUSPECTED):
            if st.phase == AgentPhase.HEALTHY:
                self.transition(agent_id, AgentPhase.SUSPECTED, reason)
            return self.transition(agent_id, AgentPhase.DRAINING, reason)
        return False

    def check_drain_timeout(self, agent_id: str) -> bool:
        """Returns True if drain has timed out and we should move to QUARANTINED."""
        st = self._state(agent_id)
        if st.phase != AgentPhase.DRAINING or st.drain_started_at is None:
            return False
        return (time.time() - st.drain_started_at) >= self.drain_timeout_s

    def complete_drain(self, agent_id: str) -> bool:
        return self.transition(agent_id, AgentPhase.QUARANTINED, "drain_complete")

    def start_healing(self, agent_id: str, reason: str = "healing_started") -> bool:
        return self.transition(agent_id, AgentPhase.HEALING, reason)

    def enter_probation(self, agent_id: str) -> bool:
        return self.transition(agent_id, AgentPhase.PROBATION, "healing_action_applied")

    def record_probation_tick(self, agent_id: str) -> int:
        """Increment probation tick counter and return the new count."""
        st = self._state(agent_id)
        if st.phase == AgentPhase.PROBATION:
            st.probation_tick_count += 1
        return st.probation_tick_count

    def probation_complete(self, agent_id: str) -> bool:
        st = self._state(agent_id)
        return (
            st.phase == AgentPhase.PROBATION
            and st.probation_tick_count >= self.probation_ticks
        )

    def mark_healthy(self, agent_id: str, reason: str = "probation_passed") -> bool:
        return self.transition(agent_id, AgentPhase.HEALTHY, reason)

    def mark_exhausted(self, agent_id: str) -> bool:
        return self.transition(agent_id, AgentPhase.EXHAUSTED, "all_actions_exhausted")

    def is_execution_allowed(self, agent_id: str) -> bool:
        """Whether the agent should be permitted to execute / receive requests."""
        phase = self.get_phase(agent_id)
        return phase in (
            AgentPhase.INITIALIZING,
            AgentPhase.HEALTHY,
            AgentPhase.SUSPECTED,
            AgentPhase.PROBATION,
        )

    def is_blocked(self, agent_id: str) -> bool:
        return not self.is_execution_allowed(agent_id)

    def get_history(self, agent_id: Optional[str] = None) -> List[TransitionEvent]:
        if agent_id is None:
            return list(self._history)
        return [e for e in self._history if e.agent_id == agent_id]

    def reset(self, agent_id: str):
        """Remove all state for an agent (e.g. deregistration)."""
        self._states.pop(agent_id, None)
