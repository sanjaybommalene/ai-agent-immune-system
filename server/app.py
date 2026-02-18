"""
Server API — REST bridge between ApiStore clients and InfluxDB.

Implements every endpoint from DOCS §6 by delegating to InfluxStore.
Run alongside InfluxDB; point the immune system client at this server
with SERVER_API_BASE_URL=http://localhost:5000.
"""
import os
import sys
import logging

from flask import Flask, jsonify, request

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from immune_system.influx_store import InfluxStore

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)-18s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("server")

app = Flask(__name__)

_INFLUX_URL = os.environ.get("INFLUXDB_URL", "http://localhost:8086")
_INFLUX_TOKEN = os.environ.get("INFLUXDB_TOKEN", "")
_INFLUX_ORG = os.environ.get("INFLUXDB_ORG", "appd")
_INFLUX_BUCKET = os.environ.get("INFLUXDB_BUCKET", "immune_system")
_SERVER_API_KEY = os.environ.get("SERVER_API_KEY", "")
_SERVER_PORT = int(os.environ.get("SERVER_PORT", "5000"))

_stores: dict = {}


def _get_store(run_id: str) -> InfluxStore:
    if run_id not in _stores:
        _stores[run_id] = InfluxStore(
            url=_INFLUX_URL,
            token=_INFLUX_TOKEN,
            org=_INFLUX_ORG,
            bucket=_INFLUX_BUCKET,
            run_id=run_id,
        )
        log.info("Created InfluxStore for run_id=%s", run_id)
    return _stores[run_id]


def _run_id() -> str:
    return request.headers.get("X-Run-Id") or request.args.get("run_id") or "default"


def _store() -> InfluxStore:
    return _get_store(_run_id())


@app.before_request
def _check_auth():
    if not _SERVER_API_KEY:
        return None
    if request.path == "/api/v1/health":
        return None
    provided = request.headers.get("X-API-Key", "")
    if not provided:
        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            provided = auth_header[7:]
    if provided != _SERVER_API_KEY:
        return jsonify({"error": "Unauthorized"}), 401
    return None


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

@app.route("/api/v1/health")
def health():
    try:
        h = _get_store("_health_check").client.health()
        if h.status == "pass":
            return jsonify({"status": "healthy"}), 200
        return jsonify({"status": h.status}), 503
    except Exception as exc:
        return jsonify({"status": "unreachable", "error": str(exc)}), 503


@app.route("/api/v1/run", methods=["POST"])
def create_run():
    from uuid import uuid4
    run_id = f"run-{uuid4().hex[:12]}"
    _get_store(run_id)
    return jsonify({"run_id": run_id}), 200


# ---------------------------------------------------------------------------
# Telemetry
# ---------------------------------------------------------------------------

@app.route("/api/v1/vitals", methods=["POST"])
def post_vitals():
    body = request.get_json(silent=True) or {}
    try:
        _store().write_agent_vitals(body)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500
    return "", 204


@app.route("/api/v1/vitals/recent")
def get_vitals_recent():
    agent_id = request.args.get("agent_id", "")
    window = float(request.args.get("window_seconds", 30))
    try:
        rows = _store().get_recent_agent_vitals(agent_id, window)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500
    return jsonify(rows), 200


@app.route("/api/v1/vitals/all")
def get_vitals_all():
    agent_id = request.args.get("agent_id", "")
    try:
        rows = _store().get_all_agent_vitals(agent_id)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500
    return jsonify(rows), 200


@app.route("/api/v1/vitals/latest")
def get_vitals_latest():
    agent_id = request.args.get("agent_id", "")
    try:
        row = _store().get_latest_agent_vitals(agent_id)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500
    if row is None:
        return "", 404
    return jsonify(row), 200


@app.route("/api/v1/vitals/execution-count")
def get_execution_count():
    agent_id = request.args.get("agent_id", "")
    try:
        count = _store().get_agent_execution_count(agent_id)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500
    return jsonify({"count": count}), 200


@app.route("/api/v1/vitals/total-executions")
def get_total_executions():
    try:
        total = _store().get_total_executions()
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500
    return jsonify({"total": total}), 200


# ---------------------------------------------------------------------------
# Baselines
# ---------------------------------------------------------------------------

@app.route("/api/v1/baselines", methods=["POST"])
def post_baseline():
    body = request.get_json(silent=True) or {}
    try:
        _store().write_baseline_profile(body)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500
    return "", 204


@app.route("/api/v1/baselines/<agent_id>")
def get_baseline(agent_id: str):
    try:
        profile = _store().get_baseline_profile(agent_id)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500
    if profile is None:
        return "", 404
    return jsonify(profile), 200


@app.route("/api/v1/baselines/count")
def get_baselines_count():
    try:
        count = _store().count_baselines()
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500
    return jsonify({"count": count}), 200


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------

@app.route("/api/v1/events/infection", methods=["POST"])
def post_infection():
    body = request.get_json(silent=True) or {}
    try:
        _store().write_infection_event(
            agent_id=body["agent_id"],
            max_deviation=float(body.get("max_deviation", 0)),
            anomalies=body.get("anomalies", []),
            deviations=body.get("deviations", {}),
            diagnosis_type=body.get("diagnosis_type", "unknown"),
        )
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500
    return "", 204


@app.route("/api/v1/events/quarantine", methods=["POST"])
def post_quarantine():
    body = request.get_json(silent=True) or {}
    try:
        _store().write_quarantine_event(
            agent_id=body["agent_id"],
            action=body["action"],
            duration_s=body.get("duration_s"),
        )
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500
    return "", 204


# ---------------------------------------------------------------------------
# Approvals
# ---------------------------------------------------------------------------

@app.route("/api/v1/approvals", methods=["POST"])
def post_approval():
    body = request.get_json(silent=True) or {}
    try:
        _store().write_approval_event(
            agent_id=body["agent_id"],
            decision=body["decision"],
            max_deviation=body.get("max_deviation"),
            anomalies=body.get("anomalies"),
            diagnosis_type=body.get("diagnosis_type"),
            reasoning=body.get("reasoning"),
            infection_payload=body.get("infection_payload"),
        )
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500
    return "", 204


@app.route("/api/v1/approvals/latest")
def get_approvals_latest():
    agent_id = request.args.get("agent_id")
    try:
        if agent_id:
            result = _store().get_latest_approval_state(agent_id)
            if result is None:
                return jsonify({}), 200
            return jsonify(result), 200
        rows = _store()._get_latest_approval_rows()
        return jsonify(rows), 200
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/v1/approvals/pending")
def get_approvals_pending():
    try:
        rows = _store().get_pending_approvals()
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500
    return jsonify(rows), 200


@app.route("/api/v1/approvals/rejected")
def get_approvals_rejected():
    try:
        rows = _store().get_rejected_approvals()
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500
    return jsonify(rows), 200


# ---------------------------------------------------------------------------
# Healing
# ---------------------------------------------------------------------------

@app.route("/api/v1/healing/events", methods=["POST"])
def post_healing_event():
    body = request.get_json(silent=True) or {}
    try:
        _store().write_healing_event(
            agent_id=body["agent_id"],
            diagnosis_type=body["diagnosis_type"],
            healing_action=body["healing_action"],
            success=bool(body.get("success", False)),
            validation_passed=bool(body.get("validation_passed", False)),
            trigger=body.get("trigger"),
            message=body.get("message"),
        )
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500
    return "", 204


@app.route("/api/v1/healing/failed-actions")
def get_failed_actions():
    agent_id = request.args.get("agent_id", "")
    diagnosis_type = request.args.get("diagnosis_type", "")
    try:
        actions = _store().get_failed_healing_actions(agent_id, diagnosis_type)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500
    return jsonify({"actions": actions}), 200


@app.route("/api/v1/healing/total")
def get_total_healings():
    try:
        total = _store().get_total_healings()
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500
    return jsonify({"total": total}), 200


@app.route("/api/v1/healing/success-rate")
def get_success_rate():
    try:
        rate = _store().get_healing_success_rate()
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500
    return jsonify({"rate": rate}), 200


@app.route("/api/v1/healing/pattern-summary")
def get_pattern_summary():
    try:
        summary = _store().get_healing_pattern_summary()
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500
    return jsonify(summary), 200


# ---------------------------------------------------------------------------
# Action log
# ---------------------------------------------------------------------------

@app.route("/api/v1/action-log", methods=["POST"])
def post_action_log():
    body = request.get_json(silent=True) or {}
    try:
        _store().write_action_log(
            action_type=body.get("action_type", "unknown"),
            agent_id=body.get("agent_id", ""),
            payload=body.get("payload", {}),
        )
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500
    return "", 204


@app.route("/api/v1/action-log/recent")
def get_recent_actions():
    limit = int(request.args.get("limit", 50))
    try:
        rows = _store().get_recent_actions(limit=limit)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500
    return jsonify(rows), 200


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    if not _INFLUX_TOKEN:
        log.error("INFLUXDB_TOKEN is required. Set it as an environment variable.")
        sys.exit(1)

    log.info("Starting server on port %d", _SERVER_PORT)
    log.info("InfluxDB: %s (org=%s, bucket=%s)", _INFLUX_URL, _INFLUX_ORG, _INFLUX_BUCKET)
    if _SERVER_API_KEY:
        log.info("Auth enabled (API key set)")
    else:
        log.info("Auth disabled (no SERVER_API_KEY)")

    app.run(host="0.0.0.0", port=_SERVER_PORT, debug=False)
