"""
Discovery Service — Auto-detect agents as they appear through the gateway.

Maintains an in-memory registry of all observed agents, recording first-seen
and last-seen timestamps, request counts, and metadata.  Emits a log event
when a previously unseen agent is discovered.
"""
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

from immune_system.logging_config import get_logger

logger = get_logger("discovery")


@dataclass
class AgentRecord:
    """Metadata for a discovered agent."""
    agent_id: str
    agent_type: str
    first_seen: float
    last_seen: float
    request_count: int = 0
    models_used: set = field(default_factory=set)
    source_ips: set = field(default_factory=set)

    def to_dict(self) -> dict:
        return {
            "agent_id": self.agent_id,
            "agent_type": self.agent_type,
            "first_seen": self.first_seen,
            "last_seen": self.last_seen,
            "request_count": self.request_count,
            "models_used": sorted(self.models_used),
            "source_ips": sorted(self.source_ips),
        }


class DiscoveryService:
    """Thread-safe registry of observed agents."""

    def __init__(self, on_new_agent: Optional[Callable[[AgentRecord], None]] = None):
        self._agents: Dict[str, AgentRecord] = {}
        self._lock = threading.Lock()
        self._on_new_agent = on_new_agent

    def observe(
        self,
        agent_id: str,
        agent_type: str = "external",
        model: str = "",
        source_ip: str = "",
    ) -> AgentRecord:
        """Record a request from *agent_id*.  Returns the agent record."""
        now = time.time()
        with self._lock:
            if agent_id not in self._agents:
                record = AgentRecord(
                    agent_id=agent_id,
                    agent_type=agent_type,
                    first_seen=now,
                    last_seen=now,
                    request_count=1,
                    models_used={model} if model else set(),
                    source_ips={source_ip} if source_ip else set(),
                )
                self._agents[agent_id] = record
                logger.info(
                    "NEW AGENT DISCOVERED: %s (type=%s, ip=%s)",
                    agent_id, agent_type, source_ip,
                )
                if self._on_new_agent:
                    self._on_new_agent(record)
                return record

            record = self._agents[agent_id]
            record.last_seen = now
            record.request_count += 1
            if model:
                record.models_used.add(model)
            if source_ip:
                record.source_ips.add(source_ip)
            if agent_type != "external" and record.agent_type == "external":
                record.agent_type = agent_type
            return record

    def get_agent(self, agent_id: str) -> Optional[AgentRecord]:
        with self._lock:
            rec = self._agents.get(agent_id)
            return rec

    def list_agents(self) -> List[dict]:
        with self._lock:
            return [r.to_dict() for r in self._agents.values()]

    def count(self) -> int:
        with self._lock:
            return len(self._agents)
