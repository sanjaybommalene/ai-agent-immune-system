"""
Quarantine Controller — Isolate infected agents using pluggable enforcement.

The controller delegates actual blocking/unblocking to an ``EnforcementStrategy``
(gateway policy injection, OS signals, container control, or a composite chain).
When no strategy is configured it falls back to in-memory tracking only.
"""
from __future__ import annotations

import asyncio
import time
from typing import Dict, Optional, Set

from .enforcement import EnforcementResult, EnforcementStrategy, NoOpEnforcement
from .logging_config import get_logger

logger = get_logger("quarantine")


class QuarantineController:
    """Manages quarantine of infected agents with real enforcement."""

    def __init__(self, enforcement: Optional[EnforcementStrategy] = None):
        self.enforcement: EnforcementStrategy = enforcement or NoOpEnforcement()
        self.quarantined: Set[str] = set()
        self.draining: Set[str] = set()
        self.quarantine_times: Dict[str, float] = {}
        self.total_quarantines = 0

    async def quarantine_async(self, agent_id: str, reason: str = "anomaly") -> EnforcementResult:
        """Quarantine with real enforcement (async).  Use for production flows."""
        result = await self.enforcement.block(agent_id, reason)
        if result.success or isinstance(self.enforcement, NoOpEnforcement):
            self._mark_quarantined(agent_id)
        else:
            logger.error("Enforcement block failed for %s: %s", agent_id, result.detail)
            self._mark_quarantined(agent_id)
        return result

    def quarantine(self, agent_id: str):
        """Synchronous quarantine (backward compat — in-memory only)."""
        self._mark_quarantined(agent_id)

    def _mark_quarantined(self, agent_id: str):
        if agent_id not in self.quarantined:
            self.quarantined.add(agent_id)
            self.quarantine_times[agent_id] = time.time()
            self.total_quarantines += 1
        self.draining.discard(agent_id)

    async def drain_async(self, agent_id: str, timeout_s: float = 30.0) -> EnforcementResult:
        """Start draining: block new requests, allow in-flight to finish."""
        self.draining.add(agent_id)
        result = await self.enforcement.drain(agent_id, timeout_s)
        self.draining.discard(agent_id)
        self._mark_quarantined(agent_id)
        return result

    async def release_async(self, agent_id: str) -> EnforcementResult:
        """Release with real enforcement (async)."""
        result = await self.enforcement.unblock(agent_id)
        self._mark_released(agent_id)
        return result

    def release(self, agent_id: str):
        """Synchronous release (backward compat — in-memory only)."""
        self._mark_released(agent_id)

    def _mark_released(self, agent_id: str):
        self.quarantined.discard(agent_id)
        self.draining.discard(agent_id)
        if agent_id in self.quarantine_times:
            del self.quarantine_times[agent_id]

    def is_quarantined(self, agent_id: str) -> bool:
        return agent_id in self.quarantined

    def is_draining(self, agent_id: str) -> bool:
        return agent_id in self.draining

    def get_quarantine_duration(self, agent_id: str) -> float:
        if agent_id not in self.quarantine_times:
            return 0.0
        return time.time() - self.quarantine_times[agent_id]

    def get_quarantined_count(self) -> int:
        return len(self.quarantined)

    def get_all_quarantined(self) -> Set[str]:
        return self.quarantined.copy()
