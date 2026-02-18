"""
Baseline Learning - Learn normal behavior for each agent
"""
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set
import statistics


@dataclass
class BaselineProfile:
    """Statistical baseline for an agent"""
    agent_id: str
    
    # Latency stats
    latency_mean: float
    latency_stddev: float
    latency_p95: float
    
    # Total token stats (backward compat)
    tokens_mean: float
    tokens_stddev: float
    tokens_p95: float
    
    # Tool call stats
    tools_mean: float
    tools_stddev: float
    tools_p95: float
    
    sample_size: int

    # Input / output token stats
    input_tokens_mean: float = 0.0
    input_tokens_stddev: float = 0.0
    input_tokens_p95: float = 0.0
    output_tokens_mean: float = 0.0
    output_tokens_stddev: float = 0.0
    output_tokens_p95: float = 0.0

    # Cost stats
    cost_mean: float = 0.0
    cost_stddev: float = 0.0
    cost_p95: float = 0.0

    # Majority prompt hash seen during baseline window
    prompt_hash: str = ""
    
    def __str__(self):
        return (f"Baseline[{self.agent_id}]: "
                f"latency={self.latency_mean:.0f}ms±{self.latency_stddev:.0f}, "
                f"tokens={self.tokens_mean:.0f}±{self.tokens_stddev:.0f}, "
                f"in={self.input_tokens_mean:.0f} out={self.output_tokens_mean:.0f}, "
                f"cost=${self.cost_mean:.4f}±{self.cost_stddev:.4f}, "
                f"tools={self.tools_mean:.1f}±{self.tools_stddev:.1f}")


class BaselineLearner:
    """Learns baseline behavior for agents"""
    
    def __init__(self, min_samples: int = 20, store=None):
        self.min_samples = min_samples
        self.store = store
        self.baselines: Dict[str, BaselineProfile] = {}
    
    def learn_baseline(self, agent_id: str, vitals_list: List) -> BaselineProfile:
        """Compute baseline from telemetry data"""
        if len(vitals_list) < self.min_samples:
            return None
        
        sample = vitals_list[:self.min_samples]

        latencies = [v.latency_ms for v in sample]
        tokens = [v.token_count for v in sample]
        tools = [v.tool_calls for v in sample]
        input_tokens = [getattr(v, 'input_tokens', 0) for v in sample]
        output_tokens = [getattr(v, 'output_tokens', 0) for v in sample]
        costs = [getattr(v, 'cost', 0.0) for v in sample]

        # Determine dominant prompt hash
        hashes = [getattr(v, 'prompt_hash', '') for v in sample if getattr(v, 'prompt_hash', '')]
        prompt_hash = max(set(hashes), key=hashes.count) if hashes else ""

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
            sample_size=len(sample),
            input_tokens_mean=statistics.mean(input_tokens),
            input_tokens_stddev=statistics.stdev(input_tokens) if len(input_tokens) > 1 else 0,
            input_tokens_p95=self._percentile(input_tokens, 95),
            output_tokens_mean=statistics.mean(output_tokens),
            output_tokens_stddev=statistics.stdev(output_tokens) if len(output_tokens) > 1 else 0,
            output_tokens_p95=self._percentile(output_tokens, 95),
            cost_mean=statistics.mean(costs),
            cost_stddev=statistics.stdev(costs) if len(costs) > 1 else 0,
            cost_p95=self._percentile(costs, 95),
            prompt_hash=prompt_hash,
        )
        
        self.baselines[agent_id] = baseline
        if self.store:
            self.store.write_baseline_profile(
                {
                    "agent_id": baseline.agent_id,
                    "latency_mean": baseline.latency_mean,
                    "latency_stddev": baseline.latency_stddev,
                    "latency_p95": baseline.latency_p95,
                    "tokens_mean": baseline.tokens_mean,
                    "tokens_stddev": baseline.tokens_stddev,
                    "tokens_p95": baseline.tokens_p95,
                    "tools_mean": baseline.tools_mean,
                    "tools_stddev": baseline.tools_stddev,
                    "tools_p95": baseline.tools_p95,
                    "sample_size": baseline.sample_size,
                    "input_tokens_mean": baseline.input_tokens_mean,
                    "input_tokens_stddev": baseline.input_tokens_stddev,
                    "input_tokens_p95": baseline.input_tokens_p95,
                    "output_tokens_mean": baseline.output_tokens_mean,
                    "output_tokens_stddev": baseline.output_tokens_stddev,
                    "output_tokens_p95": baseline.output_tokens_p95,
                    "cost_mean": baseline.cost_mean,
                    "cost_stddev": baseline.cost_stddev,
                    "cost_p95": baseline.cost_p95,
                }
            )
        return baseline
    
    def is_baseline_ready(self, agent_id: str, current_count: int) -> bool:
        """Check if enough samples collected for baseline"""
        return current_count >= self.min_samples and agent_id not in self.baselines
    
    def get_baseline(self, agent_id: str) -> BaselineProfile:
        """Get baseline for an agent"""
        if agent_id in self.baselines:
            return self.baselines[agent_id]
        if self.store:
            raw = self.store.get_baseline_profile(agent_id)
            if raw:
                baseline = BaselineProfile(**raw)
                self.baselines[agent_id] = baseline
                return baseline
        return self.baselines.get(agent_id)
    
    def has_baseline(self, agent_id: str) -> bool:
        """Check if baseline exists for agent"""
        if agent_id in self.baselines:
            return True
        if self.store:
            raw = self.store.get_baseline_profile(agent_id)
            if raw:
                self.baselines[agent_id] = BaselineProfile(**raw)
                return True
        return agent_id in self.baselines

    def count_baselines(self) -> int:
        """Get baseline count across the fleet."""
        if self.baselines:
            return len(self.baselines)
        if self.store:
            return self.store.count_baselines()
        return len(self.baselines)
    
    @staticmethod
    def _percentile(data: List[float], percentile: int) -> float:
        """Calculate percentile"""
        sorted_data = sorted(data)
        index = int(len(sorted_data) * percentile / 100)
        return sorted_data[min(index, len(sorted_data) - 1)]
