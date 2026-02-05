"""
Quarantine Controller - Isolate infected agents
"""
from typing import Set, Dict
import time


class QuarantineController:
    """Manages quarantine of infected agents"""
    
    def __init__(self):
        self.quarantined: Set[str] = set()
        self.quarantine_times: Dict[str, float] = {}
        self.total_quarantines = 0
    
    def quarantine(self, agent_id: str):
        """Place agent in quarantine"""
        if agent_id not in self.quarantined:
            self.quarantined.add(agent_id)
            self.quarantine_times[agent_id] = time.time()
            self.total_quarantines += 1
    
    def release(self, agent_id: str):
        """Release agent from quarantine"""
        if agent_id in self.quarantined:
            self.quarantined.discard(agent_id)
            if agent_id in self.quarantine_times:
                del self.quarantine_times[agent_id]
    
    def is_quarantined(self, agent_id: str) -> bool:
        """Check if agent is quarantined"""
        return agent_id in self.quarantined
    
    def get_quarantine_duration(self, agent_id: str) -> float:
        """Get how long agent has been quarantined (in seconds)"""
        if agent_id not in self.quarantine_times:
            return 0.0
        return time.time() - self.quarantine_times[agent_id]
    
    def get_quarantined_count(self) -> int:
        """Get number of currently quarantined agents"""
        return len(self.quarantined)
    
    def get_all_quarantined(self) -> Set[str]:
        """Get set of all quarantined agent IDs"""
        return self.quarantined.copy()
