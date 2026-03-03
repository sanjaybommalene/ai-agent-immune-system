"""
MCP Proxy — HTTP/SSE middleware that sits between MCP clients and MCP servers,
observing tool calls without modifying behaviour.

The proxy intercepts JSON-RPC messages on the MCP transport layer:
  - ``tools/call`` requests  -> captures tool name, arguments
  - ``tools/call`` responses -> captures result, latency, errors

These observations are emitted as supplementary "tool vitals" that enrich the
LLM-level vitals captured by the main gateway proxy.

Usage::

    # Instead of connecting an MCP client to http://localhost:3000
    # point it at the proxy:
    export MCP_SERVER_URL=http://localhost:4001

    # Start the proxy:
    python -m gateway.mcp_proxy --upstream http://localhost:3000

Environment variables:
    MCP_UPSTREAM_URL    The real MCP server URL
    MCP_PROXY_PORT      Port to listen on (default: 4001)
"""
import json
import os
import time
import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from flask import Flask, Response, jsonify, request
from flask_cors import CORS

from immune_system.logging_config import get_logger

logger = get_logger("mcp_proxy")


@dataclass
class ToolCallRecord:
    """Single observed MCP tool invocation."""
    agent_id: str
    tool_name: str
    arguments: dict
    timestamp: float
    latency_ms: int = 0
    success: bool = True
    error: str = ""


class MCPObserver:
    """Accumulates tool-call observations for analysis."""

    def __init__(self, on_tool_call: Optional[Callable[[ToolCallRecord], None]] = None):
        self._lock = threading.Lock()
        self._recent: List[ToolCallRecord] = []
        self._max_recent = 500
        self._on_tool_call = on_tool_call
        self._pending: Dict[str, Dict[str, Any]] = {}

    def record_request(self, request_id: str, agent_id: str, tool_name: str, arguments: dict):
        """Track a pending ``tools/call`` request."""
        with self._lock:
            self._pending[request_id] = {
                "agent_id": agent_id,
                "tool_name": tool_name,
                "arguments": arguments,
                "sent_at": time.time(),
            }

    def record_response(self, request_id: str, success: bool = True, error: str = ""):
        """Match a ``tools/call`` response to its request and emit a record."""
        with self._lock:
            pending = self._pending.pop(request_id, None)
        if not pending:
            return

        latency_ms = int((time.time() - pending["sent_at"]) * 1000)
        record = ToolCallRecord(
            agent_id=pending["agent_id"],
            tool_name=pending["tool_name"],
            arguments=pending["arguments"],
            timestamp=pending["sent_at"],
            latency_ms=latency_ms,
            success=success,
            error=error,
        )
        with self._lock:
            self._recent.append(record)
            if len(self._recent) > self._max_recent:
                self._recent = self._recent[-self._max_recent:]

        if self._on_tool_call:
            self._on_tool_call(record)

        logger.info(
            "TOOL CALL: agent=%s tool=%s latency=%dms success=%s",
            record.agent_id, record.tool_name, record.latency_ms, record.success,
        )

    def get_recent(self, limit: int = 50) -> List[dict]:
        with self._lock:
            return [
                {
                    "agent_id": r.agent_id,
                    "tool_name": r.tool_name,
                    "latency_ms": r.latency_ms,
                    "success": r.success,
                    "error": r.error,
                    "timestamp": r.timestamp,
                }
                for r in self._recent[-limit:]
            ]

    def get_stats(self) -> dict:
        with self._lock:
            total = len(self._recent)
            tools_used = {}
            for r in self._recent:
                tools_used[r.tool_name] = tools_used.get(r.tool_name, 0) + 1
            return {
                "total_tool_calls": total,
                "tools_used": tools_used,
                "pending_calls": len(self._pending),
            }


def _extract_agent_id(headers: dict) -> str:
    """Derive agent identity from MCP request headers."""
    explicit = (headers.get("X-Agent-ID") or "").strip()
    if explicit:
        return explicit
    auth = (headers.get("Authorization") or "").strip()
    if auth:
        import hashlib
        return f"mcp-{hashlib.sha256(auth.encode()).hexdigest()[:12]}"
    ip = (headers.get("X-Forwarded-For") or headers.get("X-Real-Ip") or "unknown").strip()
    return f"mcp-{ip}"


def create_mcp_proxy_app(upstream_url: str) -> Flask:
    """Create a Flask app that proxies MCP HTTP/SSE traffic."""

    import httpx

    observer = MCPObserver()
    client = httpx.Client(timeout=120.0)

    app = Flask(__name__)
    CORS(app)

    @app.route("/", defaults={"path": ""}, methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"])
    @app.route("/<path:path>", methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"])
    def proxy_all(path):
        url = f"{upstream_url.rstrip('/')}/{path}" if path else upstream_url
        raw = request.get_data()
        headers = dict(request.headers)
        agent_id = _extract_agent_id(headers)

        body = None
        if raw:
            try:
                body = json.loads(raw)
            except (json.JSONDecodeError, ValueError):
                pass

        if body and body.get("method") == "tools/call":
            req_id = str(body.get("id", ""))
            params = body.get("params", {})
            observer.record_request(
                request_id=req_id,
                agent_id=agent_id,
                tool_name=params.get("name", "unknown"),
                arguments=params.get("arguments", {}),
            )

        fwd_headers = {k: v for k, v in headers.items() if k.lower() not in ("host", "transfer-encoding")}

        try:
            resp = client.request(request.method, url, headers=fwd_headers, content=raw)
        except httpx.HTTPError as exc:
            return jsonify({"error": str(exc)}), 502

        resp_body = None
        try:
            resp_body = resp.json()
        except (json.JSONDecodeError, ValueError):
            pass

        if resp_body and "id" in (resp_body if isinstance(resp_body, dict) else {}):
            req_id = str(resp_body.get("id", ""))
            is_error = "error" in resp_body
            observer.record_response(
                request_id=req_id,
                success=not is_error,
                error=json.dumps(resp_body.get("error", "")) if is_error else "",
            )

        safe_headers = {
            k: v for k, v in resp.headers.items()
            if k.lower() not in ("transfer-encoding", "content-encoding", "content-length")
        }
        safe_headers["Cache-Control"] = "no-store"
        return Response(resp.content, status=resp.status_code, headers=safe_headers)

    @app.route("/mcp-proxy/health")
    def mcp_health():
        return jsonify({"status": "ok", "upstream": upstream_url})

    @app.route("/mcp-proxy/tool-calls")
    def mcp_tool_calls():
        return jsonify(observer.get_recent())

    @app.route("/mcp-proxy/stats")
    def mcp_stats():
        return jsonify(observer.get_stats())

    return app


def main():
    upstream = os.getenv("MCP_UPSTREAM_URL", "http://localhost:3000")
    port = int(os.getenv("MCP_PROXY_PORT", "4001"))
    app = create_mcp_proxy_app(upstream)
    logger.info("MCP proxy starting on port %d -> %s", port, upstream)
    app.run(host="0.0.0.0", port=port, threaded=True)


if __name__ == "__main__":
    main()
