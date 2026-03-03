"""
In-memory store implementing the same interface as InfluxStore/ApiStore.
Used for store-backed detection and run_id isolation tests (no InfluxDB/network).
"""
import time
from typing import Any, Dict, List, Optional


class InMemoryStore:
    """Store that keeps vitals, baselines, approvals, healing events in memory, keyed by run_id."""

    def __init__(self, run_id: Optional[str] = None):
        self.run_id = run_id or f"run-{int(time.time())}"
        self._vitals: List[Dict[str, Any]] = []
        self._baselines: Dict[str, Dict[str, Any]] = {}
        self._approvals: List[Dict[str, Any]] = []
        self._healing_events: List[Dict[str, Any]] = []
        self._infection_events: List[Dict[str, Any]] = []
        self._quarantine_events: List[Dict[str, Any]] = []
        self._action_log: List[Dict[str, Any]] = []

    # -------- Telemetry --------

    def write_agent_vitals(self, vitals: Dict[str, Any]) -> None:
        rec = {**vitals, "_run_id": self.run_id}
        if "timestamp" not in rec:
            rec["timestamp"] = time.time()
        self._vitals.append(rec)

    def get_recent_agent_vitals(self, agent_id: str, window_seconds: float) -> List[Dict[str, Any]]:
        cutoff = time.time() - max(1, window_seconds)
        out = []
        for v in self._vitals:
            if v.get("_run_id") != self.run_id or v.get("agent_id") != agent_id:
                continue
            ts = v.get("timestamp", 0)
            if ts >= cutoff:
                out.append({k: v[k] for k in v if k != "_run_id"})
        return sorted(out, key=lambda x: x.get("timestamp", 0))

    def get_all_agent_vitals(self, agent_id: str) -> List[Dict[str, Any]]:
        out = [v for v in self._vitals if v.get("_run_id") == self.run_id and v.get("agent_id") == agent_id]
        return [{k: x[k] for k in x if k != "_run_id"} for x in sorted(out, key=lambda x: x.get("timestamp", 0))]

    def get_latest_agent_vitals(self, agent_id: str) -> Optional[Dict[str, Any]]:
        rows = [v for v in self._vitals if v.get("_run_id") == self.run_id and v.get("agent_id") == agent_id]
        if not rows:
            return None
        latest = max(rows, key=lambda x: x.get("timestamp", 0))
        return {k: latest[k] for k in latest if k != "_run_id"}

    def get_agent_execution_count(self, agent_id: str) -> int:
        return sum(1 for v in self._vitals if v.get("_run_id") == self.run_id and v.get("agent_id") == agent_id)

    def get_total_executions(self) -> int:
        return sum(1 for v in self._vitals if v.get("_run_id") == self.run_id)

    # -------- Baselines --------

    def write_baseline_profile(self, profile: Dict[str, Any]) -> None:
        aid = profile.get("agent_id", "")
        self._baselines[aid] = {**profile}

    def get_baseline_profile(self, agent_id: str) -> Optional[Dict[str, Any]]:
        return self._baselines.get(agent_id)

    def count_baselines(self) -> int:
        return len(self._baselines)

    # -------- Infection / Quarantine --------

    def write_infection_event(
        self,
        agent_id: str,
        max_deviation: float,
        anomalies: List[str],
        deviations: Dict[str, Any],
        diagnosis_type: str,
    ) -> None:
        self._infection_events.append({
            "_run_id": self.run_id, "agent_id": agent_id, "max_deviation": max_deviation,
            "anomalies": anomalies, "deviations": deviations, "diagnosis_type": diagnosis_type,
        })

    def write_quarantine_event(self, agent_id: str, action: str, duration_s: Optional[float] = None) -> None:
        self._quarantine_events.append({"_run_id": self.run_id, "agent_id": agent_id, "action": action, "duration_s": duration_s})

    # -------- Approval workflow --------

    def write_approval_event(
        self,
        agent_id: str,
        decision: str,
        max_deviation: Optional[float] = None,
        anomalies: Optional[List[str]] = None,
        diagnosis_type: Optional[str] = None,
        reasoning: Optional[str] = None,
        infection_payload: Optional[Dict[str, Any]] = None,
    ) -> None:
        self._approvals.append({
            "_run_id": self.run_id, "agent_id": agent_id, "decision": decision,
            "max_deviation": max_deviation, "anomalies": anomalies, "diagnosis_type": diagnosis_type,
            "reasoning": reasoning, "infection_payload": infection_payload,
            "_time": time.time(),
        })

    def get_latest_approval_state(self, agent_id: str) -> Optional[Dict[str, Any]]:
        by_agent = [a for a in self._approvals if a.get("_run_id") == self.run_id and a.get("agent_id") == agent_id]
        if not by_agent:
            return None
        latest = max(by_agent, key=lambda x: x.get("_time", 0))
        return {k: latest[k] for k in latest if not k.startswith("_")}

    def get_pending_approvals(self) -> List[Dict[str, Any]]:
        # Pending is stored in orchestrator in-memory when no store; with store we derive from approval events
        by_agent: Dict[str, Dict] = {}
        for a in self._approvals:
            if a.get("_run_id") != self.run_id:
                continue
            aid = a["agent_id"]
            if a.get("decision") == "pending" or (aid not in by_agent or a.get("_time", 0) > by_agent[aid].get("_time", 0)):
                by_agent[aid] = a
        return [
            {"agent_id": k, "max_deviation": v.get("max_deviation"), "anomalies": v.get("anomalies"),
             "diagnosis_type": v.get("diagnosis_type"), "reasoning": v.get("reasoning"), "requested_at": v.get("_time")}
            for k, v in by_agent.items() if v.get("decision") == "pending"
        ]

    def get_rejected_approvals(self) -> List[Dict[str, Any]]:
        by_agent: Dict[str, Dict] = {}
        for a in self._approvals:
            if a.get("_run_id") != self.run_id:
                continue
            aid = a["agent_id"]
            if aid not in by_agent or a.get("_time", 0) > by_agent[aid].get("_time", 0):
                by_agent[aid] = a
        return [
            {"agent_id": k, "max_deviation": v.get("max_deviation"), "anomalies": v.get("anomalies"),
             "diagnosis_type": v.get("diagnosis_type"), "reasoning": v.get("reasoning"), "rejected_at": v.get("_time")}
            for k, v in by_agent.items() if v.get("decision") == "rejected"
        ]

    # -------- Healing --------

    def write_healing_event(
        self,
        agent_id: str,
        diagnosis_type: str,
        healing_action: str,
        success: bool,
        validation_passed: bool,
        trigger: Optional[str],
        message: Optional[str],
    ) -> None:
        self._healing_events.append({
            "_run_id": self.run_id, "agent_id": agent_id, "diagnosis_type": diagnosis_type,
            "healing_action": healing_action, "success": success, "validation_passed": validation_passed,
            "trigger": trigger, "message": message,
        })

    def get_failed_healing_actions(self, agent_id: str, diagnosis_type: str) -> List[str]:
        failed = [
            e["healing_action"] for e in self._healing_events
            if e.get("_run_id") == self.run_id and e.get("agent_id") == agent_id
            and e.get("diagnosis_type") == diagnosis_type and e.get("success") is False
        ]
        return list(dict.fromkeys(failed))

    def get_total_healings(self) -> int:
        return sum(1 for e in self._healing_events if e.get("_run_id") == self.run_id)

    def get_healing_success_rate(self) -> float:
        run_events = [e for e in self._healing_events if e.get("_run_id") == self.run_id]
        if not run_events:
            return 0.0
        return sum(1 for e in run_events if e.get("success")) / len(run_events)

    def get_healing_pattern_summary(self) -> Dict[str, Dict[str, Any]]:
        return {}

    # -------- Action log --------

    def write_action_log(self, action_type: str, agent_id: str, payload: Dict[str, Any]) -> None:
        self._action_log.append({"_run_id": self.run_id, "action_type": action_type, "agent_id": agent_id, "payload": payload})

    def get_recent_actions(self, limit: int = 50) -> List[Dict[str, Any]]:
        run_log = [a for a in self._action_log if a.get("_run_id") == self.run_id]
        return [{k: a[k] for k in a if k != "_run_id"} for a in run_log[-limit:]]
