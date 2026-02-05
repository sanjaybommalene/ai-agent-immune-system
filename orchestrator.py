"""
Orchestrator - Main control loop coordinating all components
"""
import asyncio
from typing import List
import time

from agents import BaseAgent
from telemetry import TelemetryCollector
from baseline import BaselineLearner
from detection import Sentinel
from diagnosis import Diagnostician
from healing import Healer
from memory import ImmuneMemory
from quarantine import QuarantineController
from chaos import ChaosInjector


# ANSI Color codes for terminal output
class Colors:
    RESET = '\033[0m'
    BOLD = '\033[1m'
    RED = '\033[91m'
    GREEN = '\033[92m'
    YELLOW = '\033[93m'
    BLUE = '\033[94m'
    MAGENTA = '\033[95m'
    CYAN = '\033[96m'


def colored(text, color):
    """Add color to text"""
    return f"{color}{text}{Colors.RESET}"


def print_flush(*args, **kwargs):
    """Print with flush"""
    print(*args, **kwargs, flush=True)


class ImmuneSystemOrchestrator:
    """Coordinates all immune system components"""
    
    def __init__(self, agents: List[BaseAgent]):
        self.agents = {agent.agent_id: agent for agent in agents}
        
        # Initialize components
        self.telemetry = TelemetryCollector()
        self.baseline_learner = BaselineLearner(min_samples=20)
        self.sentinel = Sentinel(threshold_stddev=2.5)
        self.diagnostician = Diagnostician()
        self.quarantine = QuarantineController()
        self.immune_memory = ImmuneMemory()
        self.healer = Healer(self.telemetry, self.baseline_learner, self.sentinel)
        self.chaos = ChaosInjector()
        
        # Statistics
        self.total_infections = 0
        self.total_healed = 0
        self.total_failed_healings = 0
        self.start_time = time.time()
        
        # State
        self.running = True
        self.baselines_learned = False
    
    async def run_agent_loop(self, agent: BaseAgent):
        """Continuously run an agent and emit telemetry"""
        while self.running:
            # Skip if quarantined
            if self.quarantine.is_quarantined(agent.agent_id):
                await asyncio.sleep(1)
                continue
            
            # Execute and record telemetry
            vitals = await agent.execute()
            self.telemetry.record(vitals)
            
            # Check if baseline ready to learn
            count = self.telemetry.get_count(agent.agent_id)
            if self.baseline_learner.is_baseline_ready(agent.agent_id, count):
                all_vitals = self.telemetry.get_all(agent.agent_id)
                baseline = self.baseline_learner.learn_baseline(agent.agent_id, all_vitals)
                if baseline:
                    print_flush(colored(f"📊 Baseline learned for {agent.agent_id}:", Colors.GREEN), baseline)
            
            # Small delay between executions
            await asyncio.sleep(0.5)
    
    async def sentinel_loop(self):
        """Continuously monitor for infections"""
        await asyncio.sleep(15)  # Wait for baselines to be learned
        
        print_flush(colored("\n🛡️  SENTINEL ACTIVE - Monitoring for infections...\n", Colors.CYAN + Colors.BOLD))
        self.baselines_learned = True
        
        while self.running:
            # Check each agent
            for agent_id, agent in self.agents.items():
                # Skip if already quarantined
                if self.quarantine.is_quarantined(agent_id):
                    continue
                
                # Skip if no baseline yet
                if not self.baseline_learner.has_baseline(agent_id):
                    continue
                
                # Get recent telemetry
                recent = self.telemetry.get_recent(agent_id, window_seconds=10)
                if not recent:
                    continue
                
                # Check for infection
                baseline = self.baseline_learner.get_baseline(agent_id)
                infection = self.sentinel.detect_infection(recent, baseline)
                
                if infection:
                    self.total_infections += 1
                    
                    # Print infection alert
                    print_flush(colored(f"\n🚨 INFECTION DETECTED: {agent_id}", Colors.RED + Colors.BOLD))
                    print_flush(f"   Severity: {infection.severity:.1f}/10")
                    print_flush(f"   Anomalies: {', '.join([a.value for a in infection.anomalies])}")
                    
                    # Quarantine immediately
                    self.quarantine.quarantine(agent_id)
                    agent.quarantine()
                    print_flush(colored(f"🔒 {agent_id} QUARANTINED", Colors.YELLOW))
                    
                    # Start healing process
                    asyncio.create_task(self.heal_agent(agent_id, infection))
            
            await asyncio.sleep(3)  # Check every 3 seconds
    
    async def heal_agent(self, agent_id: str, infection):
        """Heal an infected agent"""
        agent = self.agents[agent_id]
        
        # Diagnose
        baseline = self.baseline_learner.get_baseline(agent_id)
        diagnosis = self.diagnostician.diagnose(infection, baseline)
        
        print_flush(colored(f"🩺 Diagnosis for {agent_id}:", Colors.CYAN), f"{diagnosis.diagnosis_type.value} (confidence: {diagnosis.confidence:.0%})")
        print_flush(f"   Reasoning: {diagnosis.reasoning}")
        
        # Get healing policy
        policy = self.healer.get_healing_policy(diagnosis.diagnosis_type)
        policy_str = " → ".join([a.value for a in policy])
        print_flush(colored(f"📋 Healing policy:", Colors.BLUE), f"[{policy_str}]")
        
        # Get failed actions from immune memory
        failed_actions = self.immune_memory.get_failed_actions(agent_id, diagnosis.diagnosis_type)
        
        if failed_actions:
            failed_str = ", ".join([a.value for a in failed_actions])
            print_flush(colored(f"🧠 Immune memory:", Colors.MAGENTA), f"Skipping previously failed actions: {failed_str}")
        
        # Get next action to try
        next_action = self.healer.get_next_action(diagnosis.diagnosis_type, failed_actions)
        
        if not next_action:
            print_flush(colored(f"❌ All healing actions exhausted for {agent_id}", Colors.RED))
            self.quarantine.release(agent_id)
            agent.release()
            return
        
        # Attempt healing
        print_flush(colored(f"💊 Attempting healing:", Colors.GREEN), next_action.value)
        
        result = await self.healer.apply_healing(agent, next_action)
        
        # Record in immune memory
        self.immune_memory.record_healing(
            agent_id=agent_id,
            diagnosis_type=diagnosis.diagnosis_type,
            healing_action=next_action,
            success=result.validation_passed
        )
        
        if result.validation_passed:
            print_flush(colored(f"✅ HEALING SUCCESS:", Colors.GREEN + Colors.BOLD), result.message)
            print_flush(colored(f"🔓 {agent_id} released from quarantine\n", Colors.GREEN))
            self.quarantine.release(agent_id)
            agent.release()
            self.total_healed += 1
        else:
            print_flush(colored(f"❌ HEALING FAILED:", Colors.RED + Colors.BOLD), result.message)
            self.total_failed_healings += 1
            
            # Try next action in escalation ladder
            print_flush(colored(f"⚠️  Escalating to next healing action...", Colors.YELLOW))
            await asyncio.sleep(1)
            
            # Recursively try next healing
            await self.heal_agent(agent_id, infection)
    
    async def chaos_injection_schedule(self):
        """Schedule chaos injections for demo"""
        # Wait for baselines to be learned
        await asyncio.sleep(20)
        
        print_flush(colored("\n💥 CHAOS INJECTION - Simulating failures...\n", Colors.RED + Colors.BOLD))
        
        # Inject failures into 2-3 agents
        agents_list = list(self.agents.values())
        results = self.chaos.inject_random_failure(agents_list, count=2)
        
        for agent_id, infection_type in results:
            print_flush(colored(f"💉 Injected {infection_type} into {agent_id}", Colors.RED))
    
    def print_summary(self):
        """Print final summary statistics"""
        runtime = time.time() - self.start_time
        
        print("\n" + "="*70)
        print(colored("🛡️  AI AGENT IMMUNE SYSTEM - FINAL SUMMARY", Colors.CYAN + Colors.BOLD))
        print("="*70)
        
        print(f"\n{'Metric':<35} {'Value':>30}")
        print("-"*70)
        print(f"{'Runtime':<35} {runtime:.1f} seconds")
        print(f"{'Total Agents':<35} {len(self.agents)}")
        print(f"{'Total Executions':<35} {self.telemetry.total_executions}")
        print(f"{'Baselines Learned':<35} {len(self.baseline_learner.baselines)}")
        print(f"{'Total Infections Detected':<35} {self.total_infections}")
        print(f"{'Successfully Healed':<35} {self.total_healed}")
        print(f"{'Failed Healing Attempts':<35} {self.total_failed_healings}")
        print(f"{'Total Quarantines':<35} {self.quarantine.total_quarantines}")
        print(f"{'Healing Success Rate':<35} {self.immune_memory.get_success_rate():.1%}")
        print(f"{'Immune Memory Records':<35} {self.immune_memory.get_total_healings()}")
        
        # Print learned patterns
        patterns = self.immune_memory.get_pattern_summary()
        if patterns:
            print("\n" + colored("🧠 Learned Healing Patterns:", Colors.CYAN + Colors.BOLD))
            for diagnosis, info in patterns.items():
                print(f"   {diagnosis}: Best action = {info['best_action']} ({info['success_count']} successes)")
        
        print("\n" + "="*70 + "\n")
    
    async def run(self, duration_seconds: int = 120):
        """Run the immune system for specified duration"""
        print_flush("\n" + "="*70)
        print_flush(colored("🛡️  AI AGENT IMMUNE SYSTEM", Colors.CYAN + Colors.BOLD))
        print_flush(f"Running {len(self.agents)} agents with autonomous healing")
        print_flush("="*70 + "\n")
        
        # Start all agent loops
        agent_tasks = [asyncio.create_task(self.run_agent_loop(agent)) 
                      for agent in self.agents.values()]
        
        # Start sentinel
        sentinel_task = asyncio.create_task(self.sentinel_loop())
        
        # Start chaos injection
        chaos_task = asyncio.create_task(self.chaos_injection_schedule())
        
        # Run for specified duration
        await asyncio.sleep(duration_seconds)
        
        # Shutdown
        self.running = False
        print_flush(colored("\n🛑 Shutting down immune system...", Colors.YELLOW + Colors.BOLD))
        
        # Wait for tasks to complete
        for task in agent_tasks + [sentinel_task, chaos_task]:
            task.cancel()
        
        # Print summary
        self.print_summary()
