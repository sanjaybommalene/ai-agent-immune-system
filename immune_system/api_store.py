"""
API-backed store: same interface as InfluxStore but uses a remote server REST API.
Use when the immune system runs on the client and the server provides APIs to InfluxDB.
"""
from typing import Any, Dict, List, Optional

try:
    import requests
except ImportError:
    requests = None  # type: ignore


class ApiStore:
    """
    Store implementation that calls a remote server REST API for all persistence.
    Compatible with the same interface as InfluxStore so orchestrator/telemetry/baseline
    work unchanged. Configure via SERVER_API_BASE_URL (and optional SERVER_API_KEY, SERVER_RUN_ID).
    """

    def __init__(
        self,
        base_url: str,
        api_key: Optional[str] = None,
        run_id: Optional[str] = None,
        timeout: float = 30.0,
    ):
        if requests is None:
            raise RuntimeError("api_store requires 'requests'. Install with: pip install requests")
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.run_id = run_id
        self.timeout = timeout

    def _headers(self) -> Dict[str, str]:
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self.api_key:
            if self.api_key.startswith("Bearer "):
                headers["Authorization"] = self.api_key
            else:
                headers["X-API-Key"] = self.api_key
        if self.run_id:
            headers["X-Run-Id"] = self.run_id
        return headers

    def _get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Any:
        r = requests.get(
            f"{self.base_url}{path}",
            headers=self._headers(),
            params=params,
            timeout=self.timeout,
        )
        r.raise_for_status()
        if r.status_code == 204 or not r.content:
            return None
        return r.json()

    def _post(self, path: str, json: Optional[Dict[str, Any]] = None) -> None:
        r = requests.post(
            f"{self.base_url}{path}",
            headers=self._headers(),
            json=json,
            timeout=self.timeout,
        )
        r.raise_for_status()

    # -------- Telemetry --------

    def write_agent_vitals(self, vitals: Dict[str, Any]) -> None:
        input_tokens = vitals.get("input_tokens", 0)
        output_tokens = vitals.get("output_tokens", 0)
        token_count = vitals.get("token_count", 0) or (input_tokens + output_tokens)
        payload = {
            "agent_id": vitals["agent_id"],
            "agent_type": vitals.get("agent_type", "unknown"),
            "latency_ms": vitals["latency_ms"],
            "token_count": token_count,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cost": vitals.get("cost", 0.0),
            "tool_calls": vitals["tool_calls"],
            "retries": vitals["retries"],
            "success": vitals["success"],
            "model": vitals.get("model", ""),
            "error_type": vitals.get("error_type", ""),
            "prompt_hash": vitals.get("prompt_hash", ""),
        }
        if "timestamp" in vitals:
            payload["timestamp"] = vitals["timestamp"]
        self._post("/api/v1/vitals", json=payload)

    def get_recent_agent_vitals(self, agent_id: str, window_seconds: float) -> List[Dict[str, Any]]:
        data = self._get("/api/v1/vitals/recent", params={"agent_id": agent_id, "window_seconds": int(max(1, window_seconds))})
        return data if isinstance(data, list) else []

    def get_all_agent_vitals(self, agent_id: str) -> List[Dict[str, Any]]:
        data = self._get("/api/v1/vitals/all", params={"agent_id": agent_id})
        return data if isinstance(data, list) else []

    def get_latest_agent_vitals(self, agent_id: str) -> Optional[Dict[str, Any]]:
        try:
            return self._get("/api/v1/vitals/latest", params={"agent_id": agent_id})
        except Exception:
            return None

    def get_agent_execution_count(self, agent_id: str) -> int:
        data = self._get("/api/v1/vitals/execution-count", params={"agent_id": agent_id})
        return int(data.get("count", 0)) if data else 0

    def get_total_executions(self) -> int:
        data = self._get("/api/v1/vitals/total-executions")
        return int(data.get("total", 0)) if data else 0

    # -------- Baselines --------

    def write_baseline_profile(self, profile: Dict[str, Any]) -> None:
        self._post("/api/v1/baselines", json=profile)

    def get_baseline_profile(self, agent_id: str) -> Optional[Dict[str, Any]]:
        try:
            return self._get(f"/api/v1/baselines/{agent_id}")
        except Exception:
            return None

    def count_baselines(self) -> int:
        data = self._get("/api/v1/baselines/count")
        return int(data.get("count", 0)) if data else 0

    # -------- Infection / Quarantine events --------

    def write_infection_event(
        self,
        agent_id: str,
        max_deviation: float,
        anomalies: List[str],
        deviations: Dict[str, Any],
        diagnosis_type: str,
    ) -> None:
        self._post(
            "/api/v1/events/infection",
            json={
                "agent_id": agent_id,
                "max_deviation": max_deviation,
                "anomalies": anomalies,
                "deviations": deviations,
                "diagnosis_type": diagnosis_type,
            },
        )

    def write_quarantine_event(
        self,
        agent_id: str,
        action: str,
        duration_s: Optional[float] = None,
    ) -> None:
        self._post(
            "/api/v1/events/quarantine",
            json={"agent_id": agent_id, "action": action, "duration_s": duration_s},
        )

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
        self._post(
            "/api/v1/approvals",
            json={
                "agent_id": agent_id,
                "decision": decision,
                "max_deviation": max_deviation,
                "anomalies": anomalies,
                "diagnosis_type": diagnosis_type,
                "reasoning": reasoning,
                "infection_payload": infection_payload,
            },
        )

    def get_latest_approval_state(self, agent_id: str) -> Optional[Dict[str, Any]]:
        data = self._get("/api/v1/approvals/latest", params={"agent_id": agent_id})
        if not data or not isinstance(data, dict):
            return None
        if "by_agent" in data:
            return (data.get("by_agent") or {}).get(agent_id)
        return data

    def get_pending_approvals(self) -> List[Dict[str, Any]]:
        data = self._get("/api/v1/approvals/pending")
        return data if isinstance(data, list) else []

    def get_rejected_approvals(self) -> List[Dict[str, Any]]:
        data = self._get("/api/v1/approvals/rejected")
        return data if isinstance(data, list) else []

    # -------- Healing memory --------

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
        self._post(
            "/api/v1/healing/events",
            json={
                "agent_id": agent_id,
                "diagnosis_type": diagnosis_type,
                "healing_action": healing_action,
                "success": success,
                "validation_passed": validation_passed,
                "trigger": trigger,
                "message": message,
            },
        )

    def get_failed_healing_actions(self, agent_id: str, diagnosis_type: str) -> List[str]:
        data = self._get(
            "/api/v1/healing/failed-actions",
            params={"agent_id": agent_id, "diagnosis_type": diagnosis_type},
        )
        if not data or not isinstance(data, dict):
            return []
        return list(data.get("actions") or [])

    def get_total_healings(self) -> int:
        data = self._get("/api/v1/healing/total")
        return int(data.get("total", 0)) if data else 0

    def get_healing_success_rate(self) -> float:
        data = self._get("/api/v1/healing/success-rate")
        return float(data.get("rate", 0.0)) if data else 0.0

    def get_healing_pattern_summary(self) -> Dict[str, Dict[str, Any]]:
        data = self._get("/api/v1/healing/pattern-summary")
        return data if isinstance(data, dict) else {}

    # -------- Action log --------

    def write_action_log(self, action_type: str, agent_id: str, payload: Dict[str, Any]) -> None:
        self._post(
            "/api/v1/action-log",
            json={"action_type": action_type, "agent_id": agent_id, "payload": payload},
        )

    def get_recent_actions(self, limit: int = 50) -> List[Dict[str, Any]]:
        data = self._get("/api/v1/action-log/recent", params={"limit": limit})
        return data if isinstance(data, list) else []
