"""
Chaos Engineering - Controlled failure injection for demo
"""
import random
from typing import List
from agents import BaseAgent


class ChaosInjector:
    """Injects controlled failures into agents for demonstration"""
    
    def __init__(self):
        self.injected_agents = set()
    
    def inject_token_spike(self, agent: BaseAgent):
        """Inject token explosion infection"""
        agent.infect("token_explosion")
        self.injected_agents.add(agent.agent_id)
    
    def inject_tool_loop(self, agent: BaseAgent):
        """Inject tool call loop infection"""
        agent.infect("tool_loop")
        self.injected_agents.add(agent.agent_id)
    
    def inject_latency_spike(self, agent: BaseAgent):
        """Inject latency spike infection"""
        agent.infect("latency_spike")
        self.injected_agents.add(agent.agent_id)
    
    def inject_random_failure(self, agents: List[BaseAgent], count: int = 2):
        """
        Inject random failures into multiple agents
        
        Args:
            agents: Pool of agents
            count: Number of agents to infect
        """
        infection_types = [
            ("token_explosion", "TOKEN SPIKE"),
            ("tool_loop", "TOOL LOOP"),
            ("latency_spike", "LATENCY SPIKE")
        ]
        
        # Select random agents
        available = [a for a in agents if not a.infected]
        if len(available) < count:
            count = len(available)
        
        targets = random.sample(available, count)
        
        results = []
        for agent in targets:
            infection_type, name = random.choice(infection_types)
            agent.infect(infection_type)
            self.injected_agents.add(agent.agent_id)
            results.append((agent.agent_id, name))
        
        return results
    
    def is_injected(self, agent_id: str) -> bool:
        """Check if agent had chaos injection"""
        return agent_id in self.injected_agents
    
    def clear_injection(self, agent_id: str):
        """Clear injection tracking"""
        self.injected_agents.discard(agent_id)
