"""
Baseline Learning - Learn normal behavior for each agent
"""
from dataclasses import dataclass
from typing import Dict, List
import statistics


@dataclass
class BaselineProfile:
    """Statistical baseline for an agent"""
    agent_id: str
    
    # Latency stats
    latency_mean: float
    latency_stddev: float
    latency_p95: float
    
    # Token stats
    tokens_mean: float
    tokens_stddev: float
    tokens_p95: float
    
    # Tool call stats
    tools_mean: float
    tools_stddev: float
    tools_p95: float
    
    sample_size: int
    
    def __str__(self):
        return (f"Baseline[{self.agent_id}]: "
                f"latency={self.latency_mean:.0f}ms±{self.latency_stddev:.0f}, "
                f"tokens={self.tokens_mean:.0f}±{self.tokens_stddev:.0f}, "
                f"tools={self.tools_mean:.1f}±{self.tools_stddev:.1f}")


class BaselineLearner:
    """Learns baseline behavior for agents"""
    
    def __init__(self, min_samples: int = 20):
        self.min_samples = min_samples
        self.baselines: Dict[str, BaselineProfile] = {}
    
    def learn_baseline(self, agent_id: str, vitals_list: List) -> BaselineProfile:
        """Compute baseline from telemetry data"""
        if len(vitals_list) < self.min_samples:
            return None
        
        # Extract metrics
        latencies = [v.latency_ms for v in vitals_list[:self.min_samples]]
        tokens = [v.token_count for v in vitals_list[:self.min_samples]]
        tools = [v.tool_calls for v in vitals_list[:self.min_samples]]
        
        # Compute statistics
        baseline = BaselineProfile(
            agent_id=agent_id,
            latency_mean=statistics.mean(latencies),
            latency_stddev=statistics.stdev(latencies) if len(latencies) > 1 else 0,
            latency_p95=self._percentile(latencies, 95),
            tokens_mean=statistics.mean(tokens),
            tokens_stddev=statistics.stdev(tokens) if len(tokens) > 1 else 0,
            tokens_p95=self._percentile(tokens, 95),
            tools_mean=statistics.mean(tools),
            tools_stddev=statistics.stdev(tools) if len(tools) > 1 else 0,
            tools_p95=self._percentile(tools, 95),
            sample_size=len(vitals_list[:self.min_samples])
        )
        
        self.baselines[agent_id] = baseline
        return baseline
    
    def is_baseline_ready(self, agent_id: str, current_count: int) -> bool:
        """Check if enough samples collected for baseline"""
        return current_count >= self.min_samples and agent_id not in self.baselines
    
    def get_baseline(self, agent_id: str) -> BaselineProfile:
        """Get baseline for an agent"""
        return self.baselines.get(agent_id)
    
    def has_baseline(self, agent_id: str) -> bool:
        """Check if baseline exists for agent"""
        return agent_id in self.baselines
    
    @staticmethod
    def _percentile(data: List[float], percentile: int) -> float:
        """Calculate percentile"""
        sorted_data = sorted(data)
        index = int(len(sorted_data) * percentile / 100)
        return sorted_data[min(index, len(sorted_data) - 1)]
