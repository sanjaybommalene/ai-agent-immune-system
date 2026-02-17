"""
InfluxDB-backed storage for telemetry, baselines, workflow state, and healing memory.
"""
import json
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from uuid import uuid4

from influxdb_client import InfluxDBClient, Point, WritePrecision
from influxdb_client.client.write_api import SYNCHRONOUS


class InfluxStore:
    """Thin synchronous wrapper around InfluxDB for POC-scale workloads."""

    def __init__(self, url: str, token: str, org: str, bucket: str, run_id: Optional[str] = None):
        self.url = url
        self.token = token
        self.org = org
        self.bucket = bucket
        self.run_id = run_id or f"run-{uuid4().hex[:12]}"

        self.client = InfluxDBClient(url=url, token=token, org=org)
        self.write_api = self.client.write_api(write_options=SYNCHRONOUS)
        self.query_api = self.client.query_api()

    def _write(self, measurement: str, tags: Dict[str, str], fields: Dict[str, Any], timestamp: Optional[float] = None):
        point = Point(measurement)
        point.tag("run_id", self.run_id)
        for key, value in tags.items():
            if value is not None:
                point.tag(key, str(value))

        for key, value in fields.items():
            if value is None:
                continue
            if isinstance(value, bool):
                point.field(key, int(value))
            elif isinstance(value, int):
                point.field(key, int(value))
            elif isinstance(value, float):
                point.field(key, float(value))
            else:
                point.field(key, str(value))

        ts = timestamp if timestamp is not None else time.time()
        point.time(datetime.fromtimestamp(ts, tz=timezone.utc), WritePrecision.NS)
        self.write_api.write(bucket=self.bucket, org=self.org, record=point)

    def _query(self, flux: str):
        return self.query_api.query(flux, org=self.org)

    def _run_filter(self) -> str:
        return f' and r.run_id == "{self.run_id}"'

    @staticmethod
    def _safe_json_loads(value: Any, default: Any):
        if value in (None, ""):
            return default
        try:
            return json.loads(value)
        except Exception:
            return default

    # -------- Telemetry --------

    def write_agent_vitals(self, vitals: Dict[str, Any]):
        self._write(
            measurement="agent_vitals",
            tags={
                "agent_id": vitals["agent_id"],
                "agent_type": vitals.get("agent_type", "unknown"),
            },
            fields={
                "latency_ms": float(vitals["latency_ms"]),
                "token_count": float(vitals["token_count"]),
                "tool_calls": float(vitals["tool_calls"]),
                "retries": float(vitals["retries"]),
                "success": int(bool(vitals["success"])),
            },
            timestamp=vitals.get("timestamp", time.time()),
        )

    def _query_agent_vitals(self, agent_id: str, start_expr: str, descending: bool = False, limit: Optional[int] = None) -> List[Dict[str, Any]]:
        sort_desc = "true" if descending else "false"
        limit_clause = f"|> limit(n:{limit})" if limit else ""
        flux = f'''
from(bucket: "{self.bucket}")
  |> range(start: {start_expr})
  |> filter(fn: (r) => r._measurement == "agent_vitals"{self._run_filter()} and r.agent_id == "{agent_id}")
  |> pivot(rowKey:["_time"], columnKey: ["_field"], valueColumn: "_value")
  |> sort(columns:["_time"], desc:{sort_desc})
  {limit_clause}
'''
        tables = self._query(flux)
        rows: List[Dict[str, Any]] = []
        for table in tables:
            for record in table.records:
                values = record.values
                rows.append(
                    {
                        "timestamp": record.get_time().timestamp() if record.get_time() else time.time(),
                        "agent_id": values.get("agent_id", agent_id),
                        "agent_type": values.get("agent_type", "unknown"),
                        "latency_ms": int(values.get("latency_ms", 0) or 0),
                        "token_count": int(values.get("token_count", 0) or 0),
                        "tool_calls": int(values.get("tool_calls", 0) or 0),
                        "retries": int(values.get("retries", 0) or 0),
                        "success": bool(int(values.get("success", 0) or 0)),
                    }
                )
        return rows

    def get_recent_agent_vitals(self, agent_id: str, window_seconds: float) -> List[Dict[str, Any]]:
        return self._query_agent_vitals(agent_id, start_expr=f"-{max(1, int(window_seconds))}s")

    def get_all_agent_vitals(self, agent_id: str) -> List[Dict[str, Any]]:
        return self._query_agent_vitals(agent_id, start_expr="0")

    def get_latest_agent_vitals(self, agent_id: str) -> Optional[Dict[str, Any]]:
        rows = self._query_agent_vitals(agent_id, start_expr="-7d", descending=True, limit=1)
        return rows[0] if rows else None

    def get_agent_execution_count(self, agent_id: str) -> int:
        flux = f'''
from(bucket: "{self.bucket}")
  |> range(start: 0)
  |> filter(fn: (r) => r._measurement == "agent_vitals"{self._run_filter()} and r.agent_id == "{agent_id}" and r._field == "latency_ms")
  |> group()
  |> count()
'''
        tables = self._query(flux)
        for table in tables:
            for record in table.records:
                return int(record.get_value() or 0)
        return 0

    def get_total_executions(self) -> int:
        flux = f'''
from(bucket: "{self.bucket}")
  |> range(start: 0)
  |> filter(fn: (r) => r._measurement == "agent_vitals"{self._run_filter()} and r._field == "latency_ms")
  |> group()
  |> count()
'''
        tables = self._query(flux)
        for table in tables:
            for record in table.records:
                return int(record.get_value() or 0)
        return 0

    # -------- Baselines --------

    def write_baseline_profile(self, profile: Dict[str, Any]):
        self._write(
            measurement="baseline_profile",
            tags={"agent_id": profile["agent_id"]},
            fields={
                "latency_mean": profile["latency_mean"],
                "latency_stddev": profile["latency_stddev"],
                "latency_p95": profile["latency_p95"],
                "tokens_mean": profile["tokens_mean"],
                "tokens_stddev": profile["tokens_stddev"],
                "tokens_p95": profile["tokens_p95"],
                "tools_mean": profile["tools_mean"],
                "tools_stddev": profile["tools_stddev"],
                "tools_p95": profile["tools_p95"],
                "sample_size": int(profile["sample_size"]),
            },
            timestamp=time.time(),
        )

    def get_baseline_profile(self, agent_id: str) -> Optional[Dict[str, Any]]:
        flux = f'''
from(bucket: "{self.bucket}")
  |> range(start: 0)
  |> filter(fn: (r) => r._measurement == "baseline_profile"{self._run_filter()} and r.agent_id == "{agent_id}")
  |> pivot(rowKey:["_time"], columnKey:["_field"], valueColumn:"_value")
  |> sort(columns:["_time"], desc:true)
  |> limit(n:1)
'''
        tables = self._query(flux)
        for table in tables:
            for record in table.records:
                values = record.values
                return {
                    "agent_id": agent_id,
                    "latency_mean": float(values.get("latency_mean", 0.0) or 0.0),
                    "latency_stddev": float(values.get("latency_stddev", 0.0) or 0.0),
                    "latency_p95": float(values.get("latency_p95", 0.0) or 0.0),
                    "tokens_mean": float(values.get("tokens_mean", 0.0) or 0.0),
                    "tokens_stddev": float(values.get("tokens_stddev", 0.0) or 0.0),
                    "tokens_p95": float(values.get("tokens_p95", 0.0) or 0.0),
                    "tools_mean": float(values.get("tools_mean", 0.0) or 0.0),
                    "tools_stddev": float(values.get("tools_stddev", 0.0) or 0.0),
                    "tools_p95": float(values.get("tools_p95", 0.0) or 0.0),
                    "sample_size": int(values.get("sample_size", 0) or 0),
                }
        return None

    def count_baselines(self) -> int:
        flux = f'''
from(bucket: "{self.bucket}")
  |> range(start: 0)
  |> filter(fn: (r) => r._measurement == "baseline_profile"{self._run_filter()} and r._field == "sample_size")
  |> group()
  |> distinct(column: "agent_id")
  |> count(column: "_value")
'''
        tables = self._query(flux)
        for table in tables:
            for record in table.records:
                return int(record.get_value() or 0)
        return 0

    # -------- Infection / Quarantine events --------

    def write_infection_event(self, agent_id: str, severity: float, anomalies: List[str], deviations: Dict[str, Any], diagnosis_type: str):
        self._write(
            measurement="infection_event",
            tags={"agent_id": agent_id, "diagnosis_type": diagnosis_type},
            fields={
                "severity": float(severity),
                "anomalies_json": json.dumps(anomalies),
                "deviations_json": json.dumps(deviations),
                "marker": 1,
            },
            timestamp=time.time(),
        )

    def write_quarantine_event(self, agent_id: str, action: str, duration_s: Optional[float] = None):
        self._write(
            measurement="quarantine_event",
            tags={"agent_id": agent_id, "action": action},
            fields={"duration_s": float(duration_s) if duration_s is not None else None, "marker": 1},
            timestamp=time.time(),
        )

    # -------- Approval workflow --------

    def write_approval_event(
        self,
        agent_id: str,
        decision: str,
        severity: Optional[float] = None,
        anomalies: Optional[List[str]] = None,
        diagnosis_type: Optional[str] = None,
        reasoning: Optional[str] = None,
        infection_payload: Optional[Dict[str, Any]] = None,
    ):
        self._write(
            measurement="approval_event",
            tags={"agent_id": agent_id, "decision": decision},
            fields={
                "marker": 1,
                "severity": severity,
                "anomalies_json": json.dumps(anomalies) if anomalies is not None else None,
                "diagnosis_type": diagnosis_type,
                "reasoning": reasoning,
                "infection_json": json.dumps(infection_payload) if infection_payload is not None else None,
            },
            timestamp=time.time(),
        )

    def _get_latest_approval_rows(self) -> Dict[str, Dict[str, Any]]:
        flux = f'''
from(bucket: "{self.bucket}")
  |> range(start: 0)
  |> filter(fn: (r) => r._measurement == "approval_event"{self._run_filter()})
  |> pivot(rowKey:["_time"], columnKey:["_field"], valueColumn:"_value")
  |> group(columns:["agent_id"])
  |> sort(columns:["_time"], desc:true)
  |> limit(n:1)
'''
        tables = self._query(flux)
        latest_by_agent: Dict[str, Dict[str, Any]] = {}
        for table in tables:
            for record in table.records:
                values = record.values
                agent_id = values.get("agent_id")
                if not agent_id:
                    continue
                latest_by_agent[agent_id] = {
                    "agent_id": agent_id,
                    "decision": values.get("decision"),
                    "severity": float(values.get("severity", 0.0) or 0.0),
                    "anomalies": self._safe_json_loads(values.get("anomalies_json"), []),
                    "diagnosis_type": values.get("diagnosis_type", "unknown"),
                    "reasoning": values.get("reasoning", ""),
                    "infection_payload": self._safe_json_loads(values.get("infection_json"), {}),
                    "timestamp": record.get_time().timestamp() if record.get_time() else time.time(),
                }
        return latest_by_agent

    def get_latest_approval_state(self, agent_id: str) -> Optional[Dict[str, Any]]:
        return self._get_latest_approval_rows().get(agent_id)

    def get_pending_approvals(self) -> List[Dict[str, Any]]:
        latest = self._get_latest_approval_rows()
        return [
            {
                "agent_id": v["agent_id"],
                "severity": round(v["severity"], 1),
                "anomalies": v["anomalies"],
                "diagnosis_type": v["diagnosis_type"],
                "reasoning": v["reasoning"],
                "requested_at": v["timestamp"],
            }
            for v in latest.values()
            if v.get("decision") == "pending"
        ]

    def get_rejected_approvals(self) -> List[Dict[str, Any]]:
        latest = self._get_latest_approval_rows()
        return [
            {
                "agent_id": v["agent_id"],
                "severity": round(v["severity"], 1),
                "anomalies": v["anomalies"],
                "diagnosis_type": v["diagnosis_type"],
                "reasoning": v["reasoning"],
                "rejected_at": v["timestamp"],
            }
            for v in latest.values()
            if v.get("decision") == "rejected"
        ]

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
    ):
        self._write(
            measurement="healing_event",
            tags={
                "agent_id": agent_id,
                "diagnosis_type": diagnosis_type,
                "healing_action": healing_action,
                "trigger": trigger or "auto",
            },
            fields={
                "success": int(bool(success)),
                "validation_passed": int(bool(validation_passed)),
                "message": message,
                "marker": 1,
            },
            timestamp=time.time(),
        )

    def get_failed_healing_actions(self, agent_id: str, diagnosis_type: str) -> List[str]:
        flux = f'''
from(bucket: "{self.bucket}")
  |> range(start: 0)
  |> filter(fn: (r) => r._measurement == "healing_event"{self._run_filter()} and r.agent_id == "{agent_id}" and r.diagnosis_type == "{diagnosis_type}" and r._field == "validation_passed")
  |> filter(fn: (r) => r._value == 0)
'''
        tables = self._query(flux)
        actions = set()
        for table in tables:
            for record in table.records:
                action = record.values.get("healing_action")
                if action:
                    actions.add(action)
        return sorted(actions)

    def get_total_healings(self) -> int:
        flux = f'''
from(bucket: "{self.bucket}")
  |> range(start: 0)
  |> filter(fn: (r) => r._measurement == "healing_event"{self._run_filter()} and r._field == "validation_passed")
  |> count()
'''
        tables = self._query(flux)
        for table in tables:
            for record in table.records:
                return int(record.get_value() or 0)
        return 0

    def get_healing_success_rate(self) -> float:
        flux = f'''
from(bucket: "{self.bucket}")
  |> range(start: 0)
  |> filter(fn: (r) => r._measurement == "healing_event"{self._run_filter()} and r._field == "validation_passed")
'''
        tables = self._query(flux)
        total = 0
        success = 0
        for table in tables:
            for record in table.records:
                total += 1
                success += int(record.get_value() or 0)
        return (success / total) if total else 0.0

    def get_healing_pattern_summary(self) -> Dict[str, Dict[str, Any]]:
        flux = f'''
from(bucket: "{self.bucket}")
  |> range(start: 0)
  |> filter(fn: (r) => r._measurement == "healing_event"{self._run_filter()} and r._field == "validation_passed")
  |> filter(fn: (r) => r._value == 1)
'''
        tables = self._query(flux)
        counts: Dict[str, Dict[str, int]] = {}
        for table in tables:
            for record in table.records:
                diagnosis = record.values.get("diagnosis_type")
                action = record.values.get("healing_action")
                if not diagnosis or not action:
                    continue
                counts.setdefault(diagnosis, {})
                counts[diagnosis][action] = counts[diagnosis].get(action, 0) + 1

        out: Dict[str, Dict[str, Any]] = {}
        for diagnosis, action_counts in counts.items():
            best_action, best_count = max(action_counts.items(), key=lambda item: item[1])
            out[diagnosis] = {
                "best_action": best_action,
                "success_count": best_count,
            }
        return out

    # -------- Unified action log for UI --------

    def write_action_log(self, action_type: str, agent_id: str, payload: Dict[str, Any]):
        fields = {
            "marker": 1,
            "severity": payload.get("severity"),
            "diagnosis_type": payload.get("diagnosis_type"),
            "action": payload.get("action"),
            "success": int(bool(payload.get("success"))) if payload.get("success") is not None else None,
            "trigger": payload.get("trigger"),
        }
        self._write(
            measurement="action_log",
            tags={"agent_id": agent_id, "type": action_type},
            fields=fields,
            timestamp=time.time(),
        )

    def get_recent_actions(self, limit: int = 50) -> List[Dict[str, Any]]:
        flux = f'''
from(bucket: "{self.bucket}")
  |> range(start: 0)
  |> filter(fn: (r) => r._measurement == "action_log"{self._run_filter()})
  |> pivot(rowKey:["_time"], columnKey:["_field"], valueColumn:"_value")
  |> sort(columns:["_time"], desc:true)
  |> limit(n:{limit})
'''
        tables = self._query(flux)
        out = []
        for table in tables:
            for record in table.records:
                values = record.values
                success_raw = values.get("success")
                out.append(
                    {
                        "type": values.get("type"),
                        "agent_id": values.get("agent_id"),
                        "timestamp": record.get_time().timestamp() if record.get_time() else time.time(),
                        "severity": float(values.get("severity")) if values.get("severity") is not None else None,
                        "diagnosis_type": values.get("diagnosis_type"),
                        "action": values.get("action"),
                        "success": bool(int(success_raw)) if success_raw is not None else None,
                        "trigger": values.get("trigger"),
                    }
                )
        out.sort(key=lambda x: x["timestamp"])
        return out
