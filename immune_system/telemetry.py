"""
Telemetry Collection - Store and query agent vitals
"""
from dataclasses import dataclass
from typing import List, Dict, Optional
from collections import defaultdict, deque
import time

from opentelemetry import metrics

_MAX_IN_MEMORY_SAMPLES = 500


@dataclass
class AgentVitals:
    """Single telemetry data point for an agent execution."""
    timestamp: float
    agent_id: str
    agent_type: str
    latency_ms: int
    token_count: int
    tool_calls: int
    retries: int
    success: bool
    input_tokens: int = 0
    output_tokens: int = 0
    cost: float = 0.0
    model: str = ""
    error_type: str = ""
    prompt_hash: str = ""


class TelemetryCollector:
    """Collects and stores agent telemetry"""
    
    def __init__(self, store=None):
        self.store = store
        self.data: Dict[str, deque] = defaultdict(lambda: deque(maxlen=_MAX_IN_MEMORY_SAMPLES))
        self._total_executions = 0

        meter = metrics.get_meter("immune-system.telemetry")
        self._exec_counter = meter.create_counter("agent.execution.count")
        self._latency_hist = meter.create_histogram("agent.execution.latency_ms")
        self._token_hist = meter.create_histogram("agent.execution.token_count")
        self._tool_hist = meter.create_histogram("agent.execution.tool_calls")
        self._retry_hist = meter.create_histogram("agent.execution.retries")
        self._input_token_hist = meter.create_histogram("agent.execution.input_tokens")
        self._output_token_hist = meter.create_histogram("agent.execution.output_tokens")
        self._cost_hist = meter.create_histogram("agent.execution.cost")

    @property
    def total_executions(self) -> int:
        if self.store:
            return self.store.get_total_executions()
        return self._total_executions
    
    def record(self, vitals_dict: Dict):
        """Record telemetry data"""
        input_tokens = vitals_dict.get('input_tokens', 0)
        output_tokens = vitals_dict.get('output_tokens', 0)
        token_count = vitals_dict.get('token_count', input_tokens + output_tokens)

        vitals = AgentVitals(
            timestamp=vitals_dict['timestamp'],
            agent_id=vitals_dict['agent_id'],
            agent_type=vitals_dict['agent_type'],
            latency_ms=vitals_dict['latency_ms'],
            token_count=token_count,
            tool_calls=vitals_dict['tool_calls'],
            retries=vitals_dict['retries'],
            success=vitals_dict['success'],
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost=vitals_dict.get('cost', 0.0),
            model=vitals_dict.get('model', ''),
            error_type=vitals_dict.get('error_type', ''),
            prompt_hash=vitals_dict.get('prompt_hash', ''),
        )
        attributes = {"agent_id": vitals.agent_id, "agent_type": vitals.agent_type}
        self._exec_counter.add(1, attributes=attributes)
        self._latency_hist.record(vitals.latency_ms, attributes=attributes)
        self._token_hist.record(vitals.token_count, attributes=attributes)
        self._tool_hist.record(vitals.tool_calls, attributes=attributes)
        self._retry_hist.record(vitals.retries, attributes=attributes)
        self._input_token_hist.record(vitals.input_tokens, attributes=attributes)
        self._output_token_hist.record(vitals.output_tokens, attributes=attributes)
        self._cost_hist.record(vitals.cost, attributes=attributes)

        if self.store:
            self.store.write_agent_vitals(vitals_dict)
            return

        self.data[vitals.agent_id].append(vitals)
        self._total_executions += 1
    
    def get_recent(self, agent_id: str, window_seconds: float = 30) -> List[AgentVitals]:
        """Get recent telemetry within time window"""
        if self.store:
            rows = self.store.get_recent_agent_vitals(agent_id, window_seconds=window_seconds)
            return [AgentVitals(**row) for row in rows]

        if agent_id not in self.data:
            return []
        
        cutoff_time = time.time() - window_seconds
        return [v for v in self.data[agent_id] if v.timestamp >= cutoff_time]
    
    def get_all(self, agent_id: str) -> List[AgentVitals]:
        """Get all telemetry for an agent"""
        if self.store:
            rows = self.store.get_all_agent_vitals(agent_id)
            return [AgentVitals(**row) for row in rows]
        return list(self.data.get(agent_id, []))
    
    def get_count(self, agent_id: str) -> int:
        """Get number of executions for an agent"""
        if self.store:
            return self.store.get_agent_execution_count(agent_id)
        return len(self.data.get(agent_id, []))
    
    def get_latest(self, agent_id: str) -> Optional[AgentVitals]:
        """Get most recent vitals for an agent"""
        if self.store:
            row = self.store.get_latest_agent_vitals(agent_id)
            return AgentVitals(**row) if row else None
        if agent_id not in self.data or not self.data[agent_id]:
            return None
        return self.data[agent_id][-1]
