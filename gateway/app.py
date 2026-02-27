"""
Gateway App — Flask application that serves the LLM reverse proxy and runs
the immune system core in passive-observation mode.

Usage::

    # Point agents here instead of OpenAI:
    export OPENAI_BASE_URL=http://localhost:4000/v1

    # Start the gateway:
    python -m gateway.app

Environment variables:
    LLM_UPSTREAM_URL        Upstream LLM base URL (default: https://api.openai.com)
    GATEWAY_PORT            Port to listen on       (default: 4000)
    GATEWAY_POLICIES        JSON array of policy rules (optional)
    GATEWAY_PROVIDERS       JSON object of named providers (optional)
                            e.g. '{"azure":"https://myresource.openai.azure.com"}'
    INFLUXDB_URL / TOKEN / ORG / BUCKET   Persistence (optional)
    SERVER_API_BASE_URL     ApiStore target (optional, alternative to InfluxDB)
"""
import asyncio
import json as _json_mod
import os
import sys
import threading
import time

from flask import Flask, Response, jsonify, request
from flask_cors import CORS

from immune_system.baseline import BaselineLearner
from immune_system.cache import CacheManager
from immune_system.detection import Sentinel
from immune_system.enforcement import GatewayEnforcement
from immune_system.lifecycle import AgentPhase, LifecycleManager
from immune_system.logging_config import get_logger, setup_logging
from immune_system.quarantine import QuarantineController
from immune_system.telemetry import AgentVitals, TelemetryCollector

from .discovery import DiscoveryService
from .fingerprint import AgentFingerprinter
from .policy import PolicyEngine
from .proxy import LLMProxy
from .routing import ProviderRegistry, RoutingTable

logger = get_logger("gateway.app")

_DEFAULT_UPSTREAM = "https://api.openai.com"
_DEFAULT_PORT = 4000


def _build_store():
    """Create a persistence store from environment, or return None."""
    server_api = os.getenv("SERVER_API_BASE_URL")
    if server_api:
        from immune_system.api_store import ApiStore
        cache = CacheManager()
        cache.load()
        return ApiStore(
            base_url=server_api,
            api_key=os.getenv("SERVER_API_KEY"),
            run_id=os.getenv("SERVER_RUN_ID") or cache.get_run_id(),
        )

    influx_url = os.getenv("INFLUXDB_URL")
    influx_token = os.getenv("INFLUXDB_TOKEN")
    influx_org = os.getenv("INFLUXDB_ORG")
    influx_bucket = os.getenv("INFLUXDB_BUCKET")
    if influx_url and influx_token and influx_org and influx_bucket:
        from immune_system.influx_store import InfluxStore
        cache = CacheManager()
        cache.load()
        return InfluxStore(
            url=influx_url,
            token=influx_token,
            org=influx_org,
            bucket=influx_bucket,
            run_id=cache.get_run_id(),
        )
    return None


def create_app() -> Flask:
    """Application factory."""

    setup_logging()

    upstream = os.getenv("LLM_UPSTREAM_URL", _DEFAULT_UPSTREAM).rstrip("/")
    logger.info("Gateway upstream: %s", upstream)

    registry = ProviderRegistry(default_upstream=upstream)
    providers_raw = os.getenv("GATEWAY_PROVIDERS", "").strip() or "{}"
    try:
        extra_providers = _json_mod.loads(providers_raw)
        if isinstance(extra_providers, dict):
            for name, url in extra_providers.items():
                registry.register(name, url)
    except (ValueError, TypeError):
        logger.warning("GATEWAY_PROVIDERS env var is not valid JSON, ignoring")
    routing = RoutingTable(registry)

    store = _build_store()
    cache = CacheManager()
    cache.load()

    telemetry = TelemetryCollector(store=store)
    baseline_learner = BaselineLearner(min_samples=15, store=store, cache=cache)
    sentinel = Sentinel(threshold_stddev=2.5)

    fingerprinter = AgentFingerprinter()
    discovery = DiscoveryService()
    policy = PolicyEngine()

    gateway_enforcement = GatewayEnforcement(policy_engine=policy)
    quarantine = QuarantineController(enforcement=gateway_enforcement)
    lifecycle = LifecycleManager()

    def _on_vitals(vitals_dict: dict):
        """Callback invoked by the proxy after each LLM call."""
        telemetry.record(vitals_dict)
        v = AgentVitals(
            timestamp=vitals_dict["timestamp"],
            agent_id=vitals_dict["agent_id"],
            agent_type=vitals_dict["agent_type"],
            latency_ms=vitals_dict["latency_ms"],
            token_count=vitals_dict.get("token_count", 0),
            tool_calls=vitals_dict["tool_calls"],
            retries=vitals_dict["retries"],
            success=vitals_dict["success"],
            input_tokens=vitals_dict.get("input_tokens", 0),
            output_tokens=vitals_dict.get("output_tokens", 0),
            cost=vitals_dict.get("cost", 0.0),
            model=vitals_dict.get("model", ""),
            error_type=vitals_dict.get("error_type", ""),
            prompt_hash=vitals_dict.get("prompt_hash", ""),
        )
        baseline_learner.update(vitals_dict["agent_id"], v)

    proxy = LLMProxy(
        routing=routing,
        fingerprinter=fingerprinter,
        discovery=discovery,
        policy=policy,
        on_vitals=_on_vitals,
        quarantine_controller=quarantine,
    )

    app = Flask(__name__)
    CORS(app)

    # ── Proxy catch-all for /v1/* ────────────────────────────────────────

    @app.route("/v1/<path:subpath>", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"])
    def proxy_v1(subpath):
        path = f"/v1/{subpath}"
        raw_body = request.get_data()
        headers = dict(request.headers)
        remote = request.remote_addr or ""

        is_stream = False
        if raw_body:
            try:
                import json as _json
                parsed = _json.loads(raw_body)
                is_stream = parsed.get("stream", False)
            except (ValueError, TypeError):
                pass

        if is_stream:
            status, resp_headers, gen = proxy.forward_stream(
                method=request.method,
                path=path,
                headers=headers,
                body=raw_body,
                remote_addr=remote,
            )
            safe_headers = {
                k: v for k, v in resp_headers.items()
                if k.lower() not in ("transfer-encoding", "content-encoding", "content-length")
            }
            safe_headers["Cache-Control"] = "no-store"
            return Response(gen, status=status, headers=safe_headers, content_type="text/event-stream")

        status, resp_headers, resp_body = proxy.forward(
            method=request.method,
            path=path,
            headers=headers,
            body=raw_body,
            remote_addr=remote,
        )
        safe_headers = {
            k: v for k, v in resp_headers.items()
            if k.lower() not in ("transfer-encoding", "content-encoding", "content-length")
        }
        safe_headers["Cache-Control"] = "no-store"
        return Response(resp_body, status=status, headers=safe_headers)

    # ── Gateway management API ───────────────────────────────────────────

    @app.route("/health")
    def health():
        return jsonify({
            "status": "ok",
            "upstream": upstream,
            "providers": registry.list_providers(),
            "agents_discovered": discovery.count(),
        })

    @app.route("/api/gateway/agents")
    def gateway_agents():
        return jsonify(discovery.list_agents())

    @app.route("/api/gateway/policies")
    def gateway_policies():
        return jsonify(policy.list_rules())

    @app.route("/api/gateway/stats")
    def gateway_stats():
        agents = discovery.list_agents()
        total_requests = sum(a["request_count"] for a in agents)
        all_models = set()
        for a in agents:
            all_models.update(a.get("models_used", []))

        anomalies = []
        for agent_data in agents:
            aid = agent_data["agent_id"]
            bl = baseline_learner.get_baseline(aid)
            if not bl:
                continue
            recent = telemetry.get_recent(aid, window_seconds=30)
            if not recent:
                continue
            infection = sentinel.detect_infection(recent, bl)
            if infection:
                anomalies.append({
                    "agent_id": aid,
                    "max_deviation": round(infection.max_deviation, 2),
                    "anomalies": [a.value for a in infection.anomalies],
                })

        return jsonify({
            "agents_discovered": discovery.count(),
            "total_requests_proxied": total_requests,
            "baselines_learned": baseline_learner.count_baselines(),
            "total_executions": telemetry.total_executions,
            "models_observed": sorted(all_models),
            "active_anomalies": anomalies,
        })

    @app.route("/api/gateway/agent/<agent_id>/vitals")
    def agent_vitals(agent_id):
        recent = telemetry.get_recent(agent_id, window_seconds=60)
        return jsonify([
            {
                "timestamp": v.timestamp,
                "latency_ms": v.latency_ms,
                "token_count": v.token_count,
                "input_tokens": v.input_tokens,
                "output_tokens": v.output_tokens,
                "tool_calls": v.tool_calls,
                "cost": v.cost,
                "model": v.model,
                "success": v.success,
            }
            for v in recent
        ])

    @app.route("/api/gateway/agent/<agent_id>/baseline")
    def agent_baseline(agent_id):
        bl = baseline_learner.get_baseline(agent_id)
        if not bl:
            return jsonify({"ready": False})
        return jsonify({
            "ready": True,
            "sample_size": bl.sample_size,
            "latency_mean": round(bl.latency_mean, 1),
            "latency_stddev": round(bl.latency_stddev, 1),
            "tokens_mean": round(bl.tokens_mean, 1),
            "tokens_stddev": round(bl.tokens_stddev, 1),
            "cost_mean": round(bl.cost_mean, 6),
            "tools_mean": round(bl.tools_mean, 2),
        })

    # ── Provider and routing management API ─────────────────────────────

    @app.route("/api/gateway/providers", methods=["GET"])
    def list_providers():
        return jsonify(registry.list_providers())

    @app.route("/api/gateway/providers", methods=["POST"])
    def register_provider():
        data = request.get_json(silent=True) or {}
        name = (data.get("name") or "").strip()
        url = (data.get("url") or "").strip()
        if not name or not url:
            return jsonify({"error": "name and url are required"}), 400
        ok = registry.register(name, url)
        if not ok:
            return jsonify({"error": "Invalid provider name or URL"}), 400
        return jsonify({"registered": True, "name": name, "url": url.rstrip("/")}), 201

    @app.route("/api/gateway/providers/<name>", methods=["DELETE"])
    def unregister_provider(name):
        ok = registry.unregister(name)
        if not ok:
            reason = "Cannot remove the default provider" if name == "default" else "Provider not found"
            return jsonify({"error": reason}), 400
        return jsonify({"unregistered": True, "name": name})

    @app.route("/api/gateway/routes", methods=["GET"])
    def list_routes():
        return jsonify(routing.list_routes())

    @app.route("/api/gateway/routes", methods=["POST"])
    def set_route():
        data = request.get_json(silent=True) or {}
        agent_id = (data.get("agent_id") or "").strip()
        provider = (data.get("provider") or "").strip()
        if not agent_id or not provider:
            return jsonify({"error": "agent_id and provider are required"}), 400
        ok = routing.set_route(agent_id, provider)
        if not ok:
            return jsonify({"error": f"Unknown provider: {provider}"}), 400
        return jsonify({"routed": True, "agent_id": agent_id, "provider": provider}), 201

    @app.route("/api/gateway/routes/<agent_id>", methods=["DELETE"])
    def remove_route(agent_id):
        ok = routing.remove_route(agent_id)
        if not ok:
            return jsonify({"error": "No route found for agent"}), 404
        return jsonify({"removed": True, "agent_id": agent_id})

    # ── Quarantine management API ────────────────────────────────────────

    @app.route("/api/gateway/quarantine", methods=["GET"])
    def list_quarantined():
        agents = quarantine.get_all_quarantined()
        return jsonify({
            "quarantined": sorted(agents),
            "count": len(agents),
        })

    @app.route("/api/gateway/quarantine/<agent_id>", methods=["POST"])
    def quarantine_agent(agent_id):
        import asyncio as _asyncio
        reason = (request.get_json(silent=True) or {}).get("reason", "manual")
        loop = _asyncio.new_event_loop()
        try:
            result = loop.run_until_complete(quarantine.quarantine_async(agent_id, reason))
        finally:
            loop.close()
        lifecycle.force_drain(agent_id, reason)
        lifecycle.complete_drain(agent_id)
        return jsonify({
            "agent_id": agent_id,
            "quarantined": True,
            "enforcement": result.success,
            "detail": result.detail,
        })

    @app.route("/api/gateway/quarantine/<agent_id>", methods=["DELETE"])
    def release_agent(agent_id):
        import asyncio as _asyncio
        loop = _asyncio.new_event_loop()
        try:
            result = loop.run_until_complete(quarantine.release_async(agent_id))
        finally:
            loop.close()
        lifecycle.mark_healthy(agent_id, reason="manual_release")
        return jsonify({
            "agent_id": agent_id,
            "quarantined": False,
            "enforcement": result.success,
            "detail": result.detail,
        })

    @app.route("/api/gateway/lifecycle/<agent_id>", methods=["GET"])
    def agent_lifecycle(agent_id):
        phase = lifecycle.get_phase(agent_id)
        history = lifecycle.get_history(agent_id)
        return jsonify({
            "agent_id": agent_id,
            "phase": phase.value,
            "history": [
                {"from": e.from_phase.value, "to": e.to_phase.value,
                 "reason": e.reason, "timestamp": e.timestamp}
                for e in history[-20:]
            ],
        })

    logger.info("Gateway app created (port=%s, upstream=%s)", os.getenv("GATEWAY_PORT", _DEFAULT_PORT), upstream)
    return app


def main():
    port = int(os.getenv("GATEWAY_PORT", str(_DEFAULT_PORT)))
    app = create_app()
    logger.info("Starting LLM Gateway on port %d", port)
    app.run(host="0.0.0.0", port=port, threaded=True)


if __name__ == "__main__":
    main()
