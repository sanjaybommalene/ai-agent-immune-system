"""
Orchestrator — Main control loop coordinating all immune system components.

Integrates:
  - LifecycleManager    (8-state agent lifecycle with transition guards)
  - EnforcementStrategy (pluggable real quarantine: gateway / process / container)
  - HealingExecutor     (pluggable real healing actions)
  - FleetCorrelator     (detect fleet-wide vs. agent-specific anomalies)
  - Multi-hypothesis diagnosis with fallback on failure
  - Probation-based post-healing validation (fresh vitals, not stale ones)
  - Success-weighted action selection via ImmuneMemory
  - Baseline adaptation after successful healing
"""
import asyncio
import threading
from typing import List, Dict, Any, Optional, Tuple
import time
from opentelemetry import metrics

from .agents import BaseAgent, AgentStatus
from .detection import InfectionReport, AnomalyType, Sentinel
from .telemetry import TelemetryCollector
from .baseline import BaselineLearner
from .correlator import CorrelationVerdict, FleetCorrelator
from .diagnosis import Diagnostician, DiagnosisContext, DiagnosisFeedback, DiagnosisResult
from .healing import Healer
from .lifecycle import AgentPhase, LifecycleManager
from .memory import ImmuneMemory
from .quarantine import QuarantineController
from .chaos import ChaosInjector
from .logging_config import get_logger

logger = get_logger("orchestrator")

TICK_INTERVAL_SECONDS = 1.0
DEVIATION_REQUIRING_APPROVAL = 5.0
SEVERE_DEVIATION_THRESHOLD = 6.0
HEALING_STEP_DELAY_SECONDS = 1.5
DRAIN_TIMEOUT_SECONDS = 120


class ImmuneSystemOrchestrator:
    """Coordinates all immune system components."""

    def __init__(self, agents: List[BaseAgent], store=None, cache=None,
                 enforcement=None, executor=None):
        self.agents = {agent.agent_id: agent for agent in agents}
        self.store = store
        self.cache = cache

        self.telemetry = TelemetryCollector(store=store)
        self.baseline_learner = BaselineLearner(min_samples=15, store=store, cache=cache)
        self.sentinel = Sentinel(threshold_stddev=2.5)
        self.diagnostician = Diagnostician()
        self.quarantine = QuarantineController(enforcement=enforcement)
        self.immune_memory = ImmuneMemory(store=store)
        self.healer = Healer(self.telemetry, self.baseline_learner, self.sentinel,
                             executor=executor)
        self.lifecycle = LifecycleManager()
        self.correlator = FleetCorrelator()
        self.chaos = ChaosInjector()

        if cache:
            cached_q = cache.get_quarantine()
            for aid in cached_q:
                if aid in self.agents:
                    self.quarantine.quarantine(aid)
                    self.agents[aid].quarantine()
                    logger.info("Restored quarantine for %s from cache", aid)

        self.total_infections = 0
        self.total_healed = 0
        self.total_failed_healings = 0
        self.start_time = time.time()

        self.running = True
        self.baselines_learned = False

        self._pending_approvals: Dict[str, Dict[str, Any]] = {}
        self._rejected_approvals: Dict[str, Dict[str, Any]] = {}
        self._pending_lock = threading.Lock()
        self._workflow_lock = threading.Lock()

        self.healing_in_progress: set = set()
        self._healing_action_log: List[Dict[str, Any]] = []
        self._action_log_max = 80
        self._action_log_lock = threading.Lock()

        meter = metrics.get_meter("immune-system.orchestrator")
        self._infection_counter = meter.create_counter("immune.infection.detected")
        self._approval_counter = meter.create_counter("immune.approval.events")
        self._quarantine_counter = meter.create_counter("immune.quarantine.events")

    # ── Logging helpers ──────────────────────────────────────────────

    def _log_action(self, action_type: str, agent_id: str, **kwargs):
        if self.store:
            self.store.write_action_log(action_type=action_type, agent_id=agent_id, payload=kwargs)
            return
        entry = {'type': action_type, 'agent_id': agent_id, 'timestamp': time.time(), **kwargs}
        with self._action_log_lock:
            self._healing_action_log.append(entry)
            if len(self._healing_action_log) > self._action_log_max:
                self._healing_action_log = self._healing_action_log[-self._action_log_max:]

    def get_healing_actions(self) -> List[Dict[str, Any]]:
        if self.store:
            return self.store.get_recent_actions(limit=50)
        with self._action_log_lock:
            return list(self._healing_action_log[-50:])

    # ── Infection serialization ──────────────────────────────────────

    @staticmethod
    def _serialize_infection(infection: InfectionReport) -> Dict[str, Any]:
        return {
            "agent_id": infection.agent_id,
            "max_deviation": infection.max_deviation,
            "anomalies": [a.value for a in infection.anomalies],
            "deviations": infection.deviations,
        }

    def _infection_from_payload(self, agent_id: str, payload: Dict[str, Any],
                                fallback: Optional[Dict[str, Any]] = None) -> InfectionReport:
        base = payload or {}
        if not base and fallback:
            base = {
                "max_deviation": fallback.get("max_deviation", 0.0),
                "anomalies": fallback.get("anomalies", []),
                "deviations": {},
            }
        anomalies = []
        for item in base.get("anomalies", []):
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

    # ── Quarantine helpers ───────────────────────────────────────────

    def _sync_agent_phase(self, agent_id: str):
        """Sync the BaseAgent.status with the lifecycle phase."""
        phase = self.lifecycle.get_phase(agent_id)
        agent = self.agents.get(agent_id)
        if not agent:
            return
        phase_to_status = {
            AgentPhase.INITIALIZING: AgentStatus.INITIALIZING,
            AgentPhase.HEALTHY: AgentStatus.HEALTHY,
            AgentPhase.SUSPECTED: AgentStatus.SUSPECTED,
            AgentPhase.DRAINING: AgentStatus.DRAINING,
            AgentPhase.QUARANTINED: AgentStatus.QUARANTINED,
            AgentPhase.HEALING: AgentStatus.HEALING,
            AgentPhase.PROBATION: AgentStatus.PROBATION,
            AgentPhase.EXHAUSTED: AgentStatus.EXHAUSTED,
        }
        new_status = phase_to_status.get(phase)
        if new_status:
            agent.set_phase(new_status)

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
        if not agent.infected:
            return None
        infection_type = (agent.infection_type or "").lower()
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
            anomalies = [AnomalyType.LATENCY_SPIKE, AnomalyType.TOKEN_SPIKE,
                         AnomalyType.TOOL_EXPLOSION, AnomalyType.HIGH_RETRY_RATE]
            max_dev = 8.0
        else:
            anomalies = [AnomalyType.HIGH_RETRY_RATE]
            max_dev = 3.5
        deviations = {a.value: max_dev for a in anomalies}
        return InfectionReport(agent_id=agent.agent_id, max_deviation=max_dev,
                               anomalies=anomalies, deviations=deviations)

    # ── Agent loop ───────────────────────────────────────────────────

    async def run_agent_loop(self, agent: BaseAgent):
        """Run agent on a 1s tick.  Respects lifecycle blocking."""
        while self.running:
            tick_start = time.time()

            if not self.lifecycle.is_execution_allowed(agent.agent_id):
                await asyncio.sleep(TICK_INTERVAL_SECONDS)
                continue

            vitals = await agent.execute()
            self.telemetry.record(vitals)

            from .telemetry import AgentVitals
            v = AgentVitals(
                timestamp=vitals['timestamp'], agent_id=vitals['agent_id'],
                agent_type=vitals['agent_type'], latency_ms=vitals['latency_ms'],
                token_count=vitals.get('token_count', 0), tool_calls=vitals['tool_calls'],
                retries=vitals['retries'], success=vitals['success'],
                input_tokens=vitals.get('input_tokens', 0),
                output_tokens=vitals.get('output_tokens', 0),
                cost=vitals.get('cost', 0.0), model=vitals.get('model', ''),
                error_type=vitals.get('error_type', ''),
                prompt_hash=vitals.get('prompt_hash', ''),
            )
            self.baseline_learner.update(agent.agent_id, v)

            phase = self.lifecycle.get_phase(agent.agent_id)
            if phase == AgentPhase.INITIALIZING and self.baseline_learner.has_baseline(agent.agent_id):
                self.lifecycle.mark_baseline_ready(agent.agent_id)
                self._sync_agent_phase(agent.agent_id)

            if phase == AgentPhase.PROBATION:
                count = self.lifecycle.record_probation_tick(agent.agent_id)
                if self.lifecycle.probation_complete(agent.agent_id):
                    healthy = await self.healer.validate_probation(agent.agent_id)
                    if healthy:
                        self.lifecycle.mark_healthy(agent.agent_id, "probation_passed")
                        self._release_quarantine(agent)
                        self.total_healed += 1
                        self._log_action("probation_passed", agent.agent_id)
                        logger.info("PROBATION PASSED: %s released to HEALTHY", agent.agent_id)
                    else:
                        self.lifecycle.transition(agent.agent_id, AgentPhase.HEALING,
                                                  "probation_failed")
                        self._log_action("probation_failed", agent.agent_id)
                        logger.warning("PROBATION FAILED: %s back to HEALING", agent.agent_id)
                    self._sync_agent_phase(agent.agent_id)

            elapsed = time.time() - tick_start
            await asyncio.sleep(max(0.0, TICK_INTERVAL_SECONDS - elapsed))

    # ── Sentinel loop ────────────────────────────────────────────────

    async def sentinel_loop(self):
        """Continuously monitor for infections with lifecycle-aware escalation."""
        await asyncio.sleep(15)

        logger.info("SENTINEL ACTIVE - Monitoring for infections")
        self.baselines_learned = True

        while self.running:
            for agent_id, agent in self.agents.items():
                phase = self.lifecycle.get_phase(agent_id)

                if phase in (AgentPhase.QUARANTINED, AgentPhase.HEALING,
                             AgentPhase.EXHAUSTED, AgentPhase.DRAINING,
                             AgentPhase.INITIALIZING):
                    if phase == AgentPhase.DRAINING and self.lifecycle.check_drain_timeout(agent_id):
                        self.lifecycle.complete_drain(agent_id)
                        self.quarantine.quarantine(agent_id)
                        agent.quarantine()
                        self._sync_agent_phase(agent_id)
                    continue

                baseline = self.baseline_learner.get_baseline(agent_id)

                if agent.infected:
                    infection = self._fallback_infection_from_agent_state(agent)
                else:
                    if not self.baseline_learner.has_baseline(agent_id):
                        continue
                    recent = self.telemetry.get_recent(agent_id, window_seconds=10)
                    if not recent:
                        continue
                    infection = self.sentinel.detect_infection(recent, baseline)

                if infection is None:
                    if phase == AgentPhase.SUSPECTED:
                        self.lifecycle.record_anomaly_resolved(agent_id)
                        self._sync_agent_phase(agent_id)
                    continue

                if phase == AgentPhase.PROBATION:
                    continue

                if self.store:
                    latest_state = self.store.get_latest_approval_state(agent_id)
                    if latest_state and latest_state.get("decision") == "rejected":
                        continue
                else:
                    with self._pending_lock:
                        if agent_id in self._rejected_approvals:
                            continue

                if phase == AgentPhase.HEALTHY:
                    if infection.max_deviation >= SEVERE_DEVIATION_THRESHOLD:
                        self.lifecycle.force_drain(agent_id, "severe_anomaly")
                    else:
                        self.lifecycle.record_anomaly_tick(agent_id)
                    self._sync_agent_phase(agent_id)
                    if self.lifecycle.get_phase(agent_id) not in (AgentPhase.DRAINING,):
                        continue

                if phase == AgentPhase.SUSPECTED:
                    new_phase = self.lifecycle.record_anomaly_tick(agent_id)
                    self._sync_agent_phase(agent_id)
                    if new_phase != AgentPhase.DRAINING:
                        continue

                correlation = self.correlator.correlate(
                    infection,
                    self.agents,
                    self.sentinel,
                    self.baseline_learner.baselines,
                    self.telemetry,
                )

                if correlation.verdict == CorrelationVerdict.FLEET_WIDE:
                    logger.warning(
                        "FLEET-WIDE anomaly for %s — skipping quarantine (%s)",
                        agent_id, correlation.detail,
                    )
                    self._log_action("fleet_wide_anomaly", agent_id,
                                     detail=correlation.detail)
                    if self.lifecycle.get_phase(agent_id) == AgentPhase.SUSPECTED:
                        self.lifecycle.record_anomaly_resolved(agent_id)
                        self._sync_agent_phase(agent_id)
                    continue

                self.total_infections += 1
                self._infection_counter.add(
                    1, attributes={
                        "agent_id": agent_id,
                        "deviation_band": "severe" if infection.max_deviation >= DEVIATION_REQUIRING_APPROVAL else "mild",
                    },
                )

                anomaly_names = ", ".join(a.value for a in infection.anomalies)
                logger.warning(
                    "INFECTION DETECTED: %s | max_dev=%.2fσ | anomalies=[%s]",
                    agent_id, infection.max_deviation, anomaly_names,
                )

                if self.lifecycle.get_phase(agent_id) != AgentPhase.DRAINING:
                    self.lifecycle.force_drain(agent_id, "quarantine_ordered")
                self.lifecycle.complete_drain(agent_id)
                self.quarantine.quarantine(agent_id)
                agent.quarantine()
                self._sync_agent_phase(agent_id)
                self._quarantine_counter.add(1, attributes={"agent_id": agent_id, "action": "enter"})
                if self.cache:
                    self.cache.add_quarantine(agent_id)
                    self.cache.save_if_dirty()
                if self.store:
                    self.store.write_quarantine_event(agent_id=agent_id, action="enter")
                logger.warning("Agent %s QUARANTINED", agent_id)

                ctx = DiagnosisContext(
                    fleet_wide=(correlation.verdict != CorrelationVerdict.AGENT_SPECIFIC),
                    affected_fraction=correlation.affected_fraction,
                    affected_agents=correlation.affected_agents,
                    correlation_detail=correlation.detail,
                )

                if infection.max_deviation >= DEVIATION_REQUIRING_APPROVAL:
                    diagnosis_result = self.diagnostician.diagnose(infection, baseline, ctx)
                    diagnosis = diagnosis_result.primary
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
                                'diagnosis_result': diagnosis_result,
                                'requested_at': time.time(),
                            }
                    self._approval_counter.add(1, attributes={"decision": "requested", "agent_id": agent_id})
                    self._log_action("approval_requested", agent_id,
                                     max_deviation=round(infection.max_deviation, 2))
                    logger.info(
                        "Agent %s requires approval (max_dev=%.2fσ)",
                        agent_id, infection.max_deviation,
                    )
                else:
                    asyncio.create_task(self.heal_agent(agent_id, infection, context=ctx))

            await asyncio.sleep(TICK_INTERVAL_SECONDS)

    # ── Approval workflow ────────────────────────────────────────────

    @staticmethod
    def _unwrap_diagnosis(diag):
        """Extract the primary Diagnosis from a DiagnosisResult or pass through a Diagnosis."""
        if hasattr(diag, 'primary'):
            return diag.primary
        return diag

    def get_pending_approvals(self) -> List[Dict[str, Any]]:
        if self.store:
            return self.store.get_pending_approvals()
        with self._pending_lock:
            out = []
            for agent_id, data in self._pending_approvals.items():
                inf = data['infection']
                diag = self._unwrap_diagnosis(data['diagnosis'])
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
                        agent_id=agent_id, decision="approved",
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
                    agent_id=agent_id, decision="rejected",
                    max_deviation=latest.get("max_deviation"),
                    anomalies=latest.get("anomalies"),
                    diagnosis_type=latest.get("diagnosis_type"),
                    reasoning=latest.get("reasoning"),
                    infection_payload=infection_payload,
                )
                self.lifecycle.mark_exhausted(agent_id)
                self._sync_agent_phase(agent_id)
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
        self._log_action("user_rejected", agent_id)
        with self._pending_lock:
            self._rejected_approvals[agent_id] = {
                'infection': infection,
                'diagnosis': diagnosis,
                'rejected_at': time.time(),
            }
        self.lifecycle.mark_exhausted(agent_id)
        self._sync_agent_phase(agent_id)
        logger.warning("Healing rejected for %s — quarantined until 'Heal now'", agent_id)
        return None, False

    def approve_all_pending(self, approved: bool) -> List[Tuple[str, InfectionReport]]:
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
        if self.store:
            return self.store.get_rejected_approvals()
        with self._pending_lock:
            out = []
            for agent_id, data in self._rejected_approvals.items():
                inf = data['infection']
                diag = self._unwrap_diagnosis(data['diagnosis'])
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
        if self.store:
            with self._workflow_lock:
                latest = self.store.get_latest_approval_state(agent_id)
                if not latest or latest.get("decision") != "rejected":
                    return None
                infection_payload = latest.get("infection_payload", {})
                infection = self._infection_from_payload(agent_id, infection_payload, fallback=latest)
                self._approval_counter.add(1, attributes={"decision": "heal_now", "agent_id": agent_id})
                self.store.write_approval_event(
                    agent_id=agent_id, decision="heal_now",
                    max_deviation=latest.get("max_deviation"),
                    anomalies=latest.get("anomalies"),
                    diagnosis_type=latest.get("diagnosis_type"),
                    reasoning=latest.get("reasoning"),
                    infection_payload=infection_payload,
                )
                self._log_action("explicit_heal_requested", agent_id)
                return infection

        with self._pending_lock:
            entry = self._rejected_approvals.pop(agent_id, None)
        if not entry:
            return None
        infection = entry['infection']
        self._log_action("explicit_heal_requested", agent_id)
        logger.info("Agent %s — healing started (Heal now)", agent_id)
        return infection

    def start_healing_all_rejected(self) -> List[Tuple[str, InfectionReport]]:
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

    # ── Operator feedback ────────────────────────────────────────────

    def record_feedback(self, agent_id: str, actual_cause: str, notes: str = ""):
        feedback = DiagnosisFeedback(
            agent_id=agent_id,
            original_type=self.diagnostician.diagnose(
                self._fallback_infection_from_agent_state(self.agents.get(agent_id, BaseAgent(agent_id, "")))
                or InfectionReport(agent_id=agent_id, max_deviation=0, anomalies=[], deviations={}),
                self.baseline_learner.get_baseline(agent_id),
            ).primary.diagnosis_type if agent_id in self.agents else None,
            actual_cause=actual_cause,
            notes=notes,
            timestamp=time.time(),
        )
        self.diagnostician.record_feedback(feedback)
        self.immune_memory.record_feedback(feedback)
        self._log_action("operator_feedback", agent_id, actual_cause=actual_cause, notes=notes)
        logger.info("Operator feedback recorded for %s: %s", agent_id, actual_cause)

    # ── Healing (multi-hypothesis with probation) ────────────────────

    async def heal_agent(self, agent_id: str, infection: InfectionReport,
                         trigger: str = "auto", context: DiagnosisContext = None):
        """Heal using multi-hypothesis diagnosis, success-weighted selection, and probation."""
        self.healing_in_progress.add(agent_id)
        try:
            agent = self.agents[agent_id]
            baseline = self.baseline_learner.get_baseline(agent_id)
            ctx = context or DiagnosisContext()

            self.lifecycle.start_healing(agent_id)
            self._sync_agent_phase(agent_id)

            diagnosis_result = self.diagnostician.diagnose(infection, baseline, ctx)
            logger.info("Diagnosis for %s: %s", agent_id, diagnosis_result)

            await asyncio.sleep(HEALING_STEP_DELAY_SECONDS)

            for hypothesis in diagnosis_result.hypotheses:
                dtype = hypothesis.diagnosis_type
                logger.info(
                    "Trying hypothesis for %s: %s (confidence=%.0f%%)",
                    agent_id, dtype.value, hypothesis.confidence * 100,
                )

                failed_actions = self.immune_memory.get_failed_actions(agent_id, dtype)
                if failed_actions:
                    logger.info("Skipping known-failed actions for %s/%s: %s",
                                agent_id, dtype.value,
                                ", ".join(a.value for a in failed_actions))

                while True:
                    next_action = self.healer.get_next_action(dtype, failed_actions, self.immune_memory)
                    if not next_action:
                        logger.warning("All actions exhausted for %s/%s", agent_id, dtype.value)
                        break

                    logger.info("Attempting %s on %s (hypothesis=%s)",
                                next_action.value, agent_id, dtype.value)

                    result = await self.healer.apply_healing(agent, next_action)
                    await asyncio.sleep(HEALING_STEP_DELAY_SECONDS)

                    if result.success:
                        self.lifecycle.enter_probation(agent_id)
                        self._sync_agent_phase(agent_id)
                        self.quarantine.release(agent_id)

                        probation_ok = await self._run_probation(agent_id, agent)

                        self.immune_memory.record_healing(
                            agent_id=agent_id, diagnosis_type=dtype,
                            healing_action=next_action, success=probation_ok,
                        )
                        self._log_action(
                            "healing_attempt", agent_id,
                            diagnosis_type=dtype.value, action=next_action.value,
                            success=probation_ok, trigger=trigger,
                        )

                        if probation_ok:
                            logger.info("HEALING SUCCESS for %s: %s (probation passed)", agent_id, next_action.value)
                            self.lifecycle.mark_healthy(agent_id, "healed")
                            self._sync_agent_phase(agent_id)
                            self._release_quarantine(agent)
                            self.total_healed += 1
                            self.baseline_learner.accelerate_learning(agent_id)
                            return
                        else:
                            logger.warning("Probation FAILED for %s after %s", agent_id, next_action.value)
                            self.total_failed_healings += 1
                            self.quarantine.quarantine(agent_id)
                            self.lifecycle.transition(agent_id, AgentPhase.HEALING, "probation_failed")
                            self._sync_agent_phase(agent_id)
                            failed_actions = failed_actions | {next_action}
                    else:
                        self.immune_memory.record_healing(
                            agent_id=agent_id, diagnosis_type=dtype,
                            healing_action=next_action, success=False,
                        )
                        self._log_action(
                            "healing_attempt", agent_id,
                            diagnosis_type=dtype.value, action=next_action.value,
                            success=False, trigger=trigger,
                        )
                        self.total_failed_healings += 1
                        failed_actions = failed_actions | {next_action}
                        await asyncio.sleep(HEALING_STEP_DELAY_SECONDS)

            logger.error("All hypotheses and actions exhausted for %s", agent_id)
            self.lifecycle.mark_exhausted(agent_id)
            self._sync_agent_phase(agent_id)

        finally:
            self.healing_in_progress.discard(agent_id)

    async def _run_probation(self, agent_id: str, agent: BaseAgent) -> bool:
        """Run the probation loop: let agent execute, collect fresh vitals, validate."""
        ticks = self.lifecycle.probation_ticks
        for _ in range(ticks):
            if not self.running:
                return True
            await asyncio.sleep(TICK_INTERVAL_SECONDS)
            self.lifecycle.record_probation_tick(agent_id)

        return await self.healer.validate_probation(agent_id)

    # ── Chaos injection (demo) ───────────────────────────────────────

    async def chaos_injection_schedule(self, duration_seconds: int = 120):
        no_inject_after = self.start_time + max(0, duration_seconds - 5)
        agents_list = list(self.agents.values())

        await asyncio.sleep(20)
        if time.time() >= no_inject_after or not self.running:
            return
        logger.info("CHAOS INJECTION (wave 1)")
        results = self.chaos.inject_random_failure(agents_list, count=5)
        for agent_id, infection_type in results:
            logger.info("Injected %s into %s", infection_type, agent_id)

        await asyncio.sleep(25)
        if time.time() >= no_inject_after or not self.running:
            return
        available = [a for a in agents_list if not a.infected]
        if available:
            logger.info("CHAOS INJECTION (wave 2)")
            wave2 = self.chaos.inject_random_failure(available, count=min(4, len(available)))
            for agent_id, infection_type in wave2:
                logger.info("Injected %s into %s", infection_type, agent_id)

        await asyncio.sleep(25)
        if time.time() >= no_inject_after or not self.running:
            return
        available = [a for a in agents_list if not a.infected]
        if available:
            logger.info("CHAOS INJECTION (wave 3)")
            wave3 = self.chaos.inject_random_failure(available, count=min(4, len(available)))
            for agent_id, infection_type in wave3:
                logger.info("Injected %s into %s", infection_type, agent_id)

    # ── Summary / reporting ──────────────────────────────────────────

    def print_summary(self):
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

    # ── Main run loop ────────────────────────────────────────────────

    async def run(self, duration_seconds: int = 120):
        logger.info("=" * 70)
        logger.info("AI AGENT IMMUNE SYSTEM - Running %d agents with autonomous healing", len(self.agents))
        logger.info("=" * 70)

        agent_tasks = [asyncio.create_task(self.run_agent_loop(agent))
                       for agent in self.agents.values()]
        sentinel_task = asyncio.create_task(self.sentinel_loop())
        chaos_task = asyncio.create_task(self.chaos_injection_schedule(duration_seconds))

        await asyncio.sleep(duration_seconds)

        logger.info("Draining: healing all quarantined agents before shutdown")
        drain_tasks = []
        approved_list = self.approve_all_pending(True)
        for agent_id, infection in approved_list:
            drain_tasks.append(asyncio.create_task(
                self.heal_agent(agent_id, infection, trigger="drain_approve")))
        rejected_list = self.start_healing_all_rejected()
        for agent_id, infection in rejected_list:
            drain_tasks.append(asyncio.create_task(
                self.heal_agent(agent_id, infection, trigger="drain_heal_now")))
        if drain_tasks:
            await asyncio.gather(*drain_tasks)
        deadline = time.time() + DRAIN_TIMEOUT_SECONDS
        while self.healing_in_progress and time.time() < deadline:
            await asyncio.sleep(0.5)
        if self.healing_in_progress:
            logger.warning("Drain timeout: some healing still in progress")
        else:
            logger.info("All quarantined agents healed")

        self.running = False
        logger.info("Shutting down immune system")

        for task in agent_tasks + [sentinel_task, chaos_task]:
            task.cancel()

        self.print_summary()
