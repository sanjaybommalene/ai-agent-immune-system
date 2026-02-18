"""
Orchestrator - Main control loop coordinating all components
"""
import asyncio
import threading
from typing import List, Dict, Any, Optional, Tuple
import time
from opentelemetry import metrics

from .agents import BaseAgent
from .detection import InfectionReport, AnomalyType, Sentinel
from .telemetry import TelemetryCollector
from .baseline import BaselineLearner
from .diagnosis import Diagnostician
from .healing import Healer
from .memory import ImmuneMemory
from .quarantine import QuarantineController
from .chaos import ChaosInjector
from .logging_config import get_logger

logger = get_logger("orchestrator")


# Backend tick interval (seconds) - aligned with UI poll interval in web_dashboard.py
TICK_INTERVAL_SECONDS = 1.0

# Infections with max_deviation >= this threshold require UI approval before
# healing.  Lower value = more infections need manual approval.
DEVIATION_REQUIRING_APPROVAL = 5.0

# Delay between healing steps so UI can show "healing in progress"
HEALING_STEP_DELAY_SECONDS = 1.5

# Max time to wait for all quarantined agents to be healed before shutdown
DRAIN_TIMEOUT_SECONDS = 120


class ImmuneSystemOrchestrator:
    """Coordinates all immune system components"""
    
    def __init__(self, agents: List[BaseAgent], store=None, cache=None):
        self.agents = {agent.agent_id: agent for agent in agents}
        self.store = store
        self.cache = cache
        
        # Initialize components
        self.telemetry = TelemetryCollector(store=store)
        self.baseline_learner = BaselineLearner(min_samples=15, store=store, cache=cache)
        self.sentinel = Sentinel(threshold_stddev=2.5)
        self.diagnostician = Diagnostician()
        self.quarantine = QuarantineController()
        self.immune_memory = ImmuneMemory(store=store)
        self.healer = Healer(self.telemetry, self.baseline_learner, self.sentinel)
        self.chaos = ChaosInjector()

        if cache:
            cached_q = cache.get_quarantine()
            for aid in cached_q:
                if aid in self.agents:
                    self.quarantine.quarantine(aid)
                    self.agents[aid].quarantine()
                    logger.info("Restored quarantine for %s from cache", aid)
        
        # Statistics
        self.total_infections = 0
        self.total_healed = 0
        self.total_failed_healings = 0
        self.start_time = time.time()
        
        # State
        self.running = True
        self.baselines_learned = False

        # In-memory workflow state is fallback only when store is not configured.
        self._pending_approvals: Dict[str, Dict[str, Any]] = {}
        self._rejected_approvals: Dict[str, Dict[str, Any]] = {}
        self._pending_lock = threading.Lock()
        self._workflow_lock = threading.Lock()

        # Agents currently in heal_agent() for UI "healing in progress" display
        self.healing_in_progress: set = set()

        # Unified log of user/system healing actions for "Recent Healing Actions" UI (fallback mode).
        self._healing_action_log: List[Dict[str, Any]] = []
        self._action_log_max = 80
        self._action_log_lock = threading.Lock()

        meter = metrics.get_meter("immune-system.orchestrator")
        self._infection_counter = meter.create_counter("immune.infection.detected")
        self._approval_counter = meter.create_counter("immune.approval.events")
        self._quarantine_counter = meter.create_counter("immune.quarantine.events")

    def _log_action(self, action_type: str, agent_id: str, **kwargs):
        """Append a healing action for the UI (thread-safe)."""
        if self.store:
            self.store.write_action_log(action_type=action_type, agent_id=agent_id, payload=kwargs)
            return
        entry = {'type': action_type, 'agent_id': agent_id, 'timestamp': time.time(), **kwargs}
        with self._action_log_lock:
            self._healing_action_log.append(entry)
            if len(self._healing_action_log) > self._action_log_max:
                self._healing_action_log = self._healing_action_log[-self._action_log_max:]

    def get_healing_actions(self) -> List[Dict[str, Any]]:
        """Return recent healing actions (user + system) for UI (thread-safe)."""
        if self.store:
            return self.store.get_recent_actions(limit=50)
        with self._action_log_lock:
            return list(self._healing_action_log[-50:])

    @staticmethod
    def _serialize_infection(infection: InfectionReport) -> Dict[str, Any]:
        return {
            "agent_id": infection.agent_id,
            "max_deviation": infection.max_deviation,
            "anomalies": [a.value for a in infection.anomalies],
            "deviations": infection.deviations,
        }

    def _infection_from_payload(self, agent_id: str, payload: Dict[str, Any], fallback: Optional[Dict[str, Any]] = None) -> InfectionReport:
        from .detection import AnomalyType

        base = payload or {}
        if not base and fallback:
            base = {
                "max_deviation": fallback.get("max_deviation", 0.0),
                "anomalies": fallback.get("anomalies", []),
                "deviations": {},
            }

        anomaly_values = base.get("anomalies", [])
        anomalies = []
        for item in anomaly_values:
            try:
                anomalies.append(AnomalyType(item))
            except ValueError:
                continue

        return InfectionReport(
            agent_id=agent_id,
            max_deviation=float(base.get("max_deviation", 0.0) or 0.0),
            anomalies=anomalies,
            deviations=base.get("deviations", {}) or {},
        )

    def _release_quarantine(self, agent: BaseAgent):
        agent_id = agent.agent_id
        duration = self.quarantine.get_quarantine_duration(agent_id)
        self.quarantine.release(agent_id)
        agent.release()
        self._quarantine_counter.add(1, attributes={"agent_id": agent_id, "action": "release"})
        if self.cache:
            self.cache.remove_quarantine(agent_id)
            self.cache.save_if_dirty()
        if self.store:
            self.store.write_quarantine_event(agent_id=agent_id, action="release", duration_s=duration)

    @staticmethod
    def _fallback_infection_from_agent_state(agent: BaseAgent) -> Optional[InfectionReport]:
        """Fallback for demo mode: avoid agents staying forever in INFECTED state."""
        if not agent.infected:
            return None

        infection_type = (agent.infection_type or "").lower()
        anomalies: List[AnomalyType]
        max_dev: float

        if infection_type in ("token_explosion", "prompt_drift"):
            anomalies = [AnomalyType.TOKEN_SPIKE]
            max_dev = 6.0 if infection_type == "prompt_drift" else 4.5
        elif infection_type == "tool_loop":
            anomalies = [AnomalyType.TOOL_EXPLOSION]
            max_dev = 5.5
        elif infection_type == "latency_spike":
            anomalies = [AnomalyType.LATENCY_SPIKE]
            max_dev = 4.0
        elif infection_type in ("high_retry_rate", "memory_corruption"):
            anomalies = [AnomalyType.HIGH_RETRY_RATE]
            max_dev = 4.5 if infection_type == "memory_corruption" else 3.5
        elif infection_type == "full_meltdown":
            anomalies = [
                AnomalyType.LATENCY_SPIKE,
                AnomalyType.TOKEN_SPIKE,
                AnomalyType.TOOL_EXPLOSION,
                AnomalyType.HIGH_RETRY_RATE,
            ]
            max_dev = 8.0
        else:
            anomalies = [AnomalyType.HIGH_RETRY_RATE]
            max_dev = 3.5

        deviations = {a.value: max_dev for a in anomalies}
        return InfectionReport(agent_id=agent.agent_id, max_deviation=max_dev, anomalies=anomalies, deviations=deviations)
    
    async def run_agent_loop(self, agent: BaseAgent):
        """Continuously run an agent and emit telemetry on a 1s tick (synced with UI poll)."""
        while self.running:
            tick_start = time.time()
            # Skip if quarantined
            if self.quarantine.is_quarantined(agent.agent_id):
                await asyncio.sleep(TICK_INTERVAL_SECONDS)
                continue

            # Execute and record telemetry
            vitals = await agent.execute()
            self.telemetry.record(vitals)

            # Feed into EWMA baseline learner (continuously adapts)
            from .telemetry import AgentVitals
            v = AgentVitals(
                timestamp=vitals['timestamp'], agent_id=vitals['agent_id'],
                agent_type=vitals['agent_type'], latency_ms=vitals['latency_ms'],
                token_count=vitals.get('token_count', 0), tool_calls=vitals['tool_calls'],
                retries=vitals['retries'], success=vitals['success'],
                input_tokens=vitals.get('input_tokens', 0), output_tokens=vitals.get('output_tokens', 0),
                cost=vitals.get('cost', 0.0), model=vitals.get('model', ''),
                error_type=vitals.get('error_type', ''), prompt_hash=vitals.get('prompt_hash', ''),
            )
            self.baseline_learner.update(agent.agent_id, v)

            # Align to 1s tick so UI (polling every 1s) sees consistent backend state
            elapsed = time.time() - tick_start
            await asyncio.sleep(max(0.0, TICK_INTERVAL_SECONDS - elapsed))
    
    async def sentinel_loop(self):
        """Continuously monitor for infections"""
        await asyncio.sleep(15)  # Wait for baselines to be learned
        
        logger.info("SENTINEL ACTIVE - Monitoring for infections")
        self.baselines_learned = True
        
        while self.running:
            # Check each agent
            for agent_id, agent in self.agents.items():
                # Skip if already quarantined
                if self.quarantine.is_quarantined(agent_id):
                    continue
                baseline = self.baseline_learner.get_baseline(agent_id)

                # If runtime marks agent infected, force containment path immediately.
                # This prevents demo agents from lingering in INFECTED state.
                if agent.infected:
                    infection = self._fallback_infection_from_agent_state(agent)
                else:
                    # Skip non-infected agents until baseline exists.
                    if not self.baseline_learner.has_baseline(agent_id):
                        continue

                    # Get recent telemetry
                    recent = self.telemetry.get_recent(agent_id, window_seconds=10)
                    if not recent:
                        continue

                    # Statistical anomaly detection for healthy runtime state.
                    infection = self.sentinel.detect_infection(recent, baseline)
                
                if infection:
                    # Skip if user previously rejected healing — wait for "Heal now"
                    if self.store:
                        latest_state = self.store.get_latest_approval_state(agent_id)
                        if latest_state and latest_state.get("decision") == "rejected":
                            continue
                    else:
                        with self._pending_lock:
                            if agent_id in self._rejected_approvals:
                                continue

                    self.total_infections += 1
                    self._infection_counter.add(
                        1,
                        attributes={
                            "agent_id": agent_id,
                            "deviation_band": "severe" if infection.max_deviation >= DEVIATION_REQUIRING_APPROVAL else "mild",
                        },
                    )

                    anomaly_names = ", ".join(a.value for a in infection.anomalies)
                    logger.warning(
                        "INFECTION DETECTED: %s | max_dev=%.2fσ | anomalies=[%s]",
                        agent_id, infection.max_deviation, anomaly_names,
                    )

                    # Quarantine immediately
                    self.quarantine.quarantine(agent_id)
                    agent.quarantine()
                    self._quarantine_counter.add(1, attributes={"agent_id": agent_id, "action": "enter"})
                    if self.cache:
                        self.cache.add_quarantine(agent_id)
                        self.cache.save_if_dirty()
                    if self.store:
                        self.store.write_quarantine_event(agent_id=agent_id, action="enter")
                    logger.warning("Agent %s QUARANTINED", agent_id)

                    # High-deviation infections require UI approval before healing
                    if infection.max_deviation >= DEVIATION_REQUIRING_APPROVAL:
                        diagnosis = self.diagnostician.diagnose(infection, baseline)
                        if self.store:
                            payload = self._serialize_infection(infection)
                            self.store.write_infection_event(
                                agent_id=agent_id,
                                max_deviation=infection.max_deviation,
                                anomalies=payload["anomalies"],
                                deviations=payload["deviations"],
                                diagnosis_type=diagnosis.diagnosis_type.value,
                            )
                            self.store.write_approval_event(
                                agent_id=agent_id,
                                decision="pending",
                                max_deviation=infection.max_deviation,
                                anomalies=payload["anomalies"],
                                diagnosis_type=diagnosis.diagnosis_type.value,
                                reasoning=diagnosis.reasoning,
                                infection_payload=payload,
                            )
                        else:
                            with self._pending_lock:
                                self._pending_approvals[agent_id] = {
                                    'infection': infection,
                                    'diagnosis': diagnosis,
                                    'requested_at': time.time()
                                }
                        self._approval_counter.add(1, attributes={"decision": "requested", "agent_id": agent_id})
                        self._log_action("approval_requested", agent_id, max_deviation=round(infection.max_deviation, 2))
                        logger.info(
                            "Agent %s requires approval (max_dev=%.2fσ) - use dashboard to Approve/Reject",
                            agent_id, infection.max_deviation,
                        )
                    else:
                        # Auto-heal for non-severe
                        asyncio.create_task(self.heal_agent(agent_id, infection))

            # Run sentinel every 1s to stay in sync with UI poll interval
            await asyncio.sleep(TICK_INTERVAL_SECONDS)
    
    def get_pending_approvals(self) -> List[Dict[str, Any]]:
        """Return list of severe infections awaiting UI approval (thread-safe)."""
        if self.store:
            return self.store.get_pending_approvals()
        with self._pending_lock:
            out = []
            for agent_id, data in self._pending_approvals.items():
                inf = data['infection']
                diag = data['diagnosis']
                out.append({
                    'agent_id': agent_id,
                    'max_deviation': round(inf.max_deviation, 2),
                    'anomalies': [a.value for a in inf.anomalies],
                    'diagnosis_type': diag.diagnosis_type.value,
                    'reasoning': diag.reasoning,
                    'requested_at': data['requested_at'],
                })
            return out

    def approve_healing(self, agent_id: str, approved: bool) -> Tuple[Optional[InfectionReport], bool]:
        """
        Approve or reject healing for a severe infection (thread-safe).
        Returns (infection, approved). If approved, caller should schedule heal_agent(agent_id, infection).
        If rejected, agent stays quarantined until user clicks "Heal now".
        """
        if self.store:
            with self._workflow_lock:
                latest = self.store.get_latest_approval_state(agent_id)
                if not latest or latest.get("decision") != "pending":
                    return None, False

                infection_payload = latest.get("infection_payload", {})
                infection = self._infection_from_payload(agent_id, infection_payload, fallback=latest)

                if approved:
                    self._approval_counter.add(1, attributes={"decision": "approved", "agent_id": agent_id})
                    self._log_action("user_approved", agent_id)
                    self.store.write_approval_event(
                        agent_id=agent_id,
                        decision="approved",
                        max_deviation=latest.get("max_deviation"),
                        anomalies=latest.get("anomalies"),
                        diagnosis_type=latest.get("diagnosis_type"),
                        reasoning=latest.get("reasoning"),
                        infection_payload=infection_payload,
                    )
                    return infection, True

                self._approval_counter.add(1, attributes={"decision": "rejected", "agent_id": agent_id})
                self._log_action("user_rejected", agent_id)
                self.store.write_approval_event(
                    agent_id=agent_id,
                    decision="rejected",
                    max_deviation=latest.get("max_deviation"),
                    anomalies=latest.get("anomalies"),
                    diagnosis_type=latest.get("diagnosis_type"),
                    reasoning=latest.get("reasoning"),
                    infection_payload=infection_payload,
                )
                logger.info("Healing rejected for %s - quarantined until 'Heal now'", agent_id)
                return None, False

        with self._pending_lock:
            entry = self._pending_approvals.pop(agent_id, None)
        if not entry:
            return None, False
        infection = entry['infection']
        diagnosis = entry['diagnosis']
        if approved:
            self._log_action("user_approved", agent_id)
            return infection, True
        # Reject: keep quarantined, store so we don't re-prompt until user clicks Retry healing
        self._log_action("user_rejected", agent_id)
        with self._pending_lock:
            self._rejected_approvals[agent_id] = {
                'infection': infection,
                'diagnosis': diagnosis,
                'rejected_at': time.time(),
            }
        logger.warning("Healing rejected for %s - quarantined until 'Heal now' in the dashboard", agent_id)
        return None, False

    def approve_all_pending(self, approved: bool) -> List[Tuple[str, InfectionReport]]:
        """
        Approve or reject all pending approvals (thread-safe).
        Returns list of (agent_id, infection) for approved ones so caller can schedule heal_agent for each.
        """
        if self.store:
            agent_ids = [item["agent_id"] for item in self.store.get_pending_approvals()]
        else:
            with self._pending_lock:
                agent_ids = list(self._pending_approvals.keys())
        approved_list = []
        for agent_id in agent_ids:
            infection, did_approve = self.approve_healing(agent_id, approved)
            if did_approve and infection:
                approved_list.append((agent_id, infection))
        return approved_list

    def get_rejected_approvals(self) -> List[Dict[str, Any]]:
        """Return list of agents whose healing was rejected (thread-safe)."""
        if self.store:
            return self.store.get_rejected_approvals()
        with self._pending_lock:
            out = []
            for agent_id, data in self._rejected_approvals.items():
                inf = data['infection']
                diag = data['diagnosis']
                out.append({
                    'agent_id': agent_id,
                    'max_deviation': round(inf.max_deviation, 2),
                    'anomalies': [a.value for a in inf.anomalies],
                    'diagnosis_type': diag.diagnosis_type.value,
                    'reasoning': diag.reasoning,
                    'rejected_at': data['rejected_at'],
                })
            return out

    def start_healing_explicitly(self, agent_id: str) -> Optional[InfectionReport]:
        """
        Start healing directly for an agent that had healing rejected (thread-safe).
        Removes from rejected and returns the stored infection so caller can schedule heal_agent.
        Returns None if agent was not in rejected_approvals.
        """
        if self.store:
            with self._workflow_lock:
                latest = self.store.get_latest_approval_state(agent_id)
                if not latest or latest.get("decision") != "rejected":
                    return None
                infection_payload = latest.get("infection_payload", {})
                infection = self._infection_from_payload(agent_id, infection_payload, fallback=latest)
                self._approval_counter.add(1, attributes={"decision": "heal_now", "agent_id": agent_id})
                self.store.write_approval_event(
                    agent_id=agent_id,
                    decision="heal_now",
                    max_deviation=latest.get("max_deviation"),
                    anomalies=latest.get("anomalies"),
                    diagnosis_type=latest.get("diagnosis_type"),
                    reasoning=latest.get("reasoning"),
                    infection_payload=infection_payload,
                )
                self._log_action("explicit_heal_requested", agent_id)
                logger.info("%s - healing started (Heal now)", agent_id)
                return infection

        with self._pending_lock:
            entry = self._rejected_approvals.pop(agent_id, None)
        if not entry:
            return None
        infection = entry['infection']
        self._log_action("explicit_heal_requested", agent_id)
        logger.info("Agent %s - healing started (Heal now)", agent_id)
        return infection

    def start_healing_all_rejected(self) -> List[Tuple[str, InfectionReport]]:
        """
        Start healing for all rejected agents (thread-safe).
        Removes all from rejected and returns list of (agent_id, infection) so caller can schedule heal_agent for each.
        """
        if self.store:
            agent_ids = [item["agent_id"] for item in self.store.get_rejected_approvals()]
        else:
            with self._pending_lock:
                agent_ids = list(self._rejected_approvals.keys())
        result = []
        for agent_id in agent_ids:
            infection = self.start_healing_explicitly(agent_id)
            if infection:
                result.append((agent_id, infection))
        return result

    async def heal_agent(self, agent_id: str, infection: InfectionReport, trigger: str = "auto"):
        """Heal an infected agent (with visible delays so UI can show progress)."""
        self.healing_in_progress.add(agent_id)
        try:
            agent = self.agents[agent_id]

            # Diagnose
            baseline = self.baseline_learner.get_baseline(agent_id)
            diagnosis = self.diagnostician.diagnose(infection, baseline)

            logger.info(
                "Diagnosis for %s: %s (confidence: %.0f%%) - %s",
                agent_id, diagnosis.diagnosis_type.value, diagnosis.confidence * 100, diagnosis.reasoning,
            )

            await asyncio.sleep(HEALING_STEP_DELAY_SECONDS)  # So UI shows "healing in progress"

            # Get healing policy
            policy = self.healer.get_healing_policy(diagnosis.diagnosis_type)
            policy_str = " -> ".join(a.value for a in policy)
            logger.info("Healing policy for %s: [%s]", agent_id, policy_str)

            # Get failed actions from immune memory
            failed_actions = self.immune_memory.get_failed_actions(agent_id, diagnosis.diagnosis_type)

            if failed_actions:
                failed_str = ", ".join(a.value for a in failed_actions)
                logger.info("Immune memory for %s: skipping previously failed actions: %s", agent_id, failed_str)

            await asyncio.sleep(HEALING_STEP_DELAY_SECONDS)

            # Get next action to try
            next_action = self.healer.get_next_action(diagnosis.diagnosis_type, failed_actions)

            if not next_action:
                logger.error("All healing actions exhausted for %s", agent_id)
                self._release_quarantine(agent)
                return

            logger.info("Attempting healing on %s: %s", agent_id, next_action.value)

            result = await self.healer.apply_healing(agent, next_action)

            # Record in immune memory
            self.immune_memory.record_healing(
                agent_id=agent_id,
                diagnosis_type=diagnosis.diagnosis_type,
                healing_action=next_action,
                success=result.validation_passed
            )
            self._log_action(
                "healing_attempt", agent_id,
                diagnosis_type=diagnosis.diagnosis_type.value,
                action=next_action.value,
                success=result.validation_passed,
                trigger=trigger
            )

            if result.validation_passed:
                logger.info("HEALING SUCCESS for %s: %s - released from quarantine", agent_id, result.message)
                self._release_quarantine(agent)
                self.total_healed += 1
            else:
                logger.warning("HEALING FAILED for %s: %s", agent_id, result.message)
                self.total_failed_healings += 1

                logger.info("Escalating to next healing action for %s", agent_id)
                await asyncio.sleep(HEALING_STEP_DELAY_SECONDS)

                await self.heal_agent(agent_id, infection, trigger=trigger)
        finally:
            self.healing_in_progress.discard(agent_id)
    
    async def chaos_injection_schedule(self, duration_seconds: int = 120):
        """Schedule chaos injections for demo. No new infections in last 5 sec so drain can reach 100% success."""
        no_inject_after = self.start_time + max(0, duration_seconds - 5)
        agents_list = list(self.agents.values())
        
        # Wait for baselines to be learned
        await asyncio.sleep(20)
        if time.time() >= no_inject_after or not self.running:
            return
        logger.info("CHAOS INJECTION (wave 1) - Simulating failures")
        results = self.chaos.inject_random_failure(agents_list, count=5)
        for agent_id, infection_type in results:
            logger.info("Injected %s into %s", infection_type, agent_id)
        
        # Second wave
        await asyncio.sleep(25)
        if time.time() >= no_inject_after or not self.running:
            return
        available = [a for a in agents_list if not a.infected]
        if available:
            logger.info("CHAOS INJECTION (wave 2) - More failures")
            wave2 = self.chaos.inject_random_failure(available, count=min(4, len(available)))
            for agent_id, infection_type in wave2:
                logger.info("Injected %s into %s", infection_type, agent_id)
        
        # Third wave — more chances for pending approvals
        await asyncio.sleep(25)
        if time.time() >= no_inject_after or not self.running:
            return
        available = [a for a in agents_list if not a.infected]
        if available:
            logger.info("CHAOS INJECTION (wave 3) - More failures")
            wave3 = self.chaos.inject_random_failure(available, count=min(4, len(available)))
            for agent_id, infection_type in wave3:
                logger.info("Injected %s into %s", infection_type, agent_id)
    
    def print_summary(self):
        """Log final summary statistics"""
        runtime = time.time() - self.start_time
        resolution_rate = (self.total_healed / self.total_infections) if self.total_infections else 0.0

        summary_lines = [
            "",
            "=" * 70,
            "AI AGENT IMMUNE SYSTEM - FINAL SUMMARY",
            "=" * 70,
            f"  {'Runtime':<35} {runtime:.1f} seconds",
            f"  {'Total Agents':<35} {len(self.agents)}",
            f"  {'Total Executions':<35} {self.telemetry.total_executions}",
            f"  {'Baselines Learned':<35} {self.baseline_learner.count_baselines()}",
            f"  {'Total Infections Detected':<35} {self.total_infections}",
            f"  {'Successfully Healed':<35} {self.total_healed}",
            f"  {'Failed Healing Attempts':<35} {self.total_failed_healings}",
            f"  {'Total Quarantine Events':<35} {self.quarantine.total_quarantines}",
            f"  {'Currently in Quarantine':<35} {self.quarantine.get_quarantined_count()}",
            f"  {'Healing Success Rate':<35} {resolution_rate:.1%}",
            f"  {'Immune Memory Records':<35} {self.immune_memory.get_total_healings()}",
        ]

        patterns = self.immune_memory.get_pattern_summary()
        if patterns:
            summary_lines.append("")
            summary_lines.append("  Learned Healing Patterns:")
            for diagnosis, info in patterns.items():
                summary_lines.append(
                    f"    {diagnosis}: best_action={info['best_action']} ({info['success_count']} successes)"
                )

        summary_lines.append("=" * 70)
        logger.info("\n".join(summary_lines))
    
    async def run(self, duration_seconds: int = 120):
        """Run the immune system for specified duration"""
        logger.info("=" * 70)
        logger.info("AI AGENT IMMUNE SYSTEM - Running %d agents with autonomous healing", len(self.agents))
        logger.info("=" * 70)
        
        # Start all agent loops
        agent_tasks = [asyncio.create_task(self.run_agent_loop(agent)) 
                      for agent in self.agents.values()]
        
        # Start sentinel
        sentinel_task = asyncio.create_task(self.sentinel_loop())
        
        # Start chaos injection (no new infections in last 5 sec)
        chaos_task = asyncio.create_task(self.chaos_injection_schedule(duration_seconds))
        
        # Run for specified duration
        await asyncio.sleep(duration_seconds)
        
        # Drain: heal all quarantined so success rate can reach 100% before closing
        logger.info("Draining: healing all quarantined agents before shutdown")
        drain_tasks = []
        approved_list = self.approve_all_pending(True)
        for agent_id, infection in approved_list:
            drain_tasks.append(asyncio.create_task(self.heal_agent(agent_id, infection, "drain_approve")))
        rejected_list = self.start_healing_all_rejected()
        for agent_id, infection in rejected_list:
            drain_tasks.append(asyncio.create_task(self.heal_agent(agent_id, infection, "drain_heal_now")))
        if drain_tasks:
            await asyncio.gather(*drain_tasks)
        deadline = time.time() + DRAIN_TIMEOUT_SECONDS
        while self.healing_in_progress and time.time() < deadline:
            await asyncio.sleep(0.5)
        if self.healing_in_progress:
            logger.warning("Drain timeout: some healing still in progress")
        else:
            logger.info("All quarantined agents healed")
        
        # Shutdown
        self.running = False
        logger.info("Shutting down immune system")
        
        # Wait for tasks to complete
        for task in agent_tasks + [sentinel_task, chaos_task]:
            task.cancel()
        
        # Print summary
        self.print_summary()
