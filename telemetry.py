"""
Telemetry Collection - Store and query agent vitals
"""
from dataclasses import dataclass
from typing import List, Dict
from collections import defaultdict
import time


@dataclass
class AgentVitals:
    """Single telemetry data point"""
    timestamp: float
    agent_id: str
    agent_type: str
    latency_ms: int
    token_count: int
    tool_calls: int
    retries: int
    success: bool


class TelemetryCollector:
    """Collects and stores agent telemetry"""
    
    def __init__(self):
        # Store telemetry per agent: {agent_id: [AgentVitals]}
        self.data: Dict[str, List[AgentVitals]] = defaultdict(list)
        self.total_executions = 0
    
    def record(self, vitals_dict: Dict):
        """Record telemetry data"""
        vitals = AgentVitals(
            timestamp=vitals_dict['timestamp'],
            agent_id=vitals_dict['agent_id'],
            agent_type=vitals_dict['agent_type'],
            latency_ms=vitals_dict['latency_ms'],
            token_count=vitals_dict['token_count'],
            tool_calls=vitals_dict['tool_calls'],
            retries=vitals_dict['retries'],
            success=vitals_dict['success']
        )
        self.data[vitals.agent_id].append(vitals)
        self.total_executions += 1
    
    def get_recent(self, agent_id: str, window_seconds: float = 30) -> List[AgentVitals]:
        """Get recent telemetry within time window"""
        if agent_id not in self.data:
            return []
        
        cutoff_time = time.time() - window_seconds
        return [v for v in self.data[agent_id] if v.timestamp >= cutoff_time]
    
    def get_all(self, agent_id: str) -> List[AgentVitals]:
        """Get all telemetry for an agent"""
        return self.data.get(agent_id, [])
    
    def get_count(self, agent_id: str) -> int:
        """Get number of executions for an agent"""
        return len(self.data.get(agent_id, []))
    
    def get_latest(self, agent_id: str) -> AgentVitals:
        """Get most recent vitals for an agent"""
        if agent_id not in self.data or not self.data[agent_id]:
            return None
        return self.data[agent_id][-1]
