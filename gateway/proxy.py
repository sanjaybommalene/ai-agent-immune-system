"""
LLM Proxy — Reverse-proxy core that forwards requests to upstream LLM
providers, passively extracts vitals, and feeds them into the immune system.

Supports:
  - OpenAI  (``https://api.openai.com``)
  - Azure OpenAI  (``https://<resource>.openai.azure.com``)
  - Any OpenAI-compatible endpoint (vLLM, LiteLLM, Ollama, etc.)

Both non-streaming and streaming (SSE) responses are handled.  For streaming,
the proxy injects ``stream_options.include_usage = true`` so the final chunk
carries token counts.
"""
import json
import time
from typing import Any, Callable, Dict, Generator, List, Optional, Tuple

import httpx

from immune_system.logging_config import get_logger

from .discovery import DiscoveryService
from .fingerprint import AgentFingerprinter
from .policy import PolicyAction, PolicyDecision, PolicyEngine
from .routing import RoutingTable
from .vitals_extractor import extract_vitals, extract_vitals_from_stream_chunks

logger = get_logger("proxy")

_HOP_BY_HOP = frozenset({
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade",
})

_UPSTREAM_TIMEOUT = 120.0


class LLMProxy:
    """Stateless proxy core.  Call :meth:`forward` per request."""

    def __init__(
        self,
        routing: RoutingTable,
        fingerprinter: AgentFingerprinter,
        discovery: DiscoveryService,
        policy: PolicyEngine,
        on_vitals: Optional[Callable[[Dict[str, Any]], None]] = None,
        timeout: float = _UPSTREAM_TIMEOUT,
        quarantine_controller=None,
    ):
        self.routing = routing
        self.fingerprinter = fingerprinter
        self.discovery = discovery
        self.policy = policy
        self.on_vitals = on_vitals
        self.quarantine = quarantine_controller
        self._client = httpx.Client(timeout=timeout, follow_redirects=True)

    @property
    def upstream(self) -> str:
        """Default upstream URL (backward-compat for /health endpoint)."""
        return self.routing.registry.default

    def close(self):
        self._client.close()

    def _resolve_upstream(self, agent_id: str, headers: dict, path: str) -> str:
        """Build the full upstream URL using the three-tier routing chain."""
        provider_hint = (headers.get("X-LLM-Provider") or headers.get("x-llm-provider") or "").strip()
        base = self.routing.resolve(agent_id, provider_hint)
        return f"{base}{path}"

    @staticmethod
    def _forward_headers(incoming: dict) -> dict:
        """Build headers for the upstream request, stripping hop-by-hop
        and the gateway-internal ``X-LLM-Provider`` header."""
        out = {}
        for k, v in incoming.items():
            if k.lower() in _HOP_BY_HOP:
                continue
            if k.lower() == "host":
                continue
            if k.lower() == "x-llm-provider":
                continue
            out[k] = v
        return out

    def forward(
        self,
        *,
        method: str,
        path: str,
        headers: dict,
        body: Optional[bytes],
        remote_addr: str = "",
    ) -> Tuple[int, dict, bytes]:
        """Forward a non-streaming request.

        Returns ``(status_code, response_headers, response_body_bytes)``.
        """

        agent_id = self.fingerprinter.identify(headers=headers, remote_addr=remote_addr)
        agent_type = self.fingerprinter.derive_agent_type(agent_id, headers)

        if self.quarantine and self.quarantine.is_quarantined(agent_id):
            err = {"error": {"message": "Agent is quarantined by immune system", "type": "quarantined"}}
            logger.warning("QUARANTINE BLOCK: agent=%s", agent_id)
            return 503, {"Content-Type": "application/json"}, json.dumps(err).encode()

        request_body = self._parse_body(body)
        model = request_body.get("model", "") if request_body else ""

        self.discovery.observe(agent_id=agent_id, agent_type=agent_type, model=model, source_ip=remote_addr)

        decision = self.policy.evaluate(agent_id, model)
        if decision.action in (PolicyAction.BLOCK, PolicyAction.THROTTLE):
            status = 429 if decision.action == PolicyAction.THROTTLE else 403
            err = {"error": {"message": decision.reason, "type": "policy_violation", "rule": decision.rule_name}}
            logger.warning("POLICY %s: agent=%s reason=%s", decision.action.value.upper(), agent_id, decision.reason)
            return status, {"Content-Type": "application/json"}, json.dumps(err).encode()

        fwd_headers = self._forward_headers(headers)
        url = self._resolve_upstream(agent_id, headers, path)

        t0 = time.time()
        try:
            resp = self._client.request(method, url, headers=fwd_headers, content=body)
            latency_ms = int((time.time() - t0) * 1000)
        except httpx.HTTPError as exc:
            latency_ms = int((time.time() - t0) * 1000)
            self._emit_vitals(
                request_body=request_body or {},
                response_body=None,
                latency_ms=latency_ms,
                agent_id=agent_id,
                agent_type=agent_type,
                success=False,
                error_type=type(exc).__name__,
            )
            err = {"error": {"message": f"Upstream error: {exc}", "type": "proxy_error"}}
            return 502, {"Content-Type": "application/json"}, json.dumps(err).encode()

        resp_body_bytes = resp.content
        resp_headers = dict(resp.headers)

        success = 200 <= resp.status_code < 400
        response_body = self._parse_body(resp_body_bytes)
        error_type = ""
        if not success and response_body:
            error_type = response_body.get("error", {}).get("type", f"http_{resp.status_code}")

        self._emit_vitals(
            request_body=request_body or {},
            response_body=response_body,
            latency_ms=latency_ms,
            agent_id=agent_id,
            agent_type=agent_type,
            success=success,
            error_type=error_type,
        )

        self.policy.record_usage(
            agent_id,
            tokens=(response_body or {}).get("usage", {}).get("total_tokens", 0),
        )

        if decision.action == PolicyAction.ALERT:
            logger.warning("POLICY ALERT: agent=%s rule=%s reason=%s", agent_id, decision.rule_name, decision.reason)

        return resp.status_code, resp_headers, resp_body_bytes

    def forward_stream(
        self,
        *,
        method: str,
        path: str,
        headers: dict,
        body: Optional[bytes],
        remote_addr: str = "",
    ) -> Tuple[int, dict, Generator[bytes, None, None]]:
        """Forward a streaming (SSE) request.

        Returns ``(status_code, response_headers, chunk_generator)``.
        The generator yields raw bytes to relay to the client.
        """

        agent_id = self.fingerprinter.identify(headers=headers, remote_addr=remote_addr)
        agent_type = self.fingerprinter.derive_agent_type(agent_id, headers)

        if self.quarantine and self.quarantine.is_quarantined(agent_id):
            err = {"error": {"message": "Agent is quarantined by immune system", "type": "quarantined"}}
            logger.warning("QUARANTINE BLOCK (stream): agent=%s", agent_id)

            def _q_gen():
                yield json.dumps(err).encode()
            return 503, {"Content-Type": "application/json"}, _q_gen()

        request_body = self._parse_body(body) or {}
        model = request_body.get("model", "")

        request_body.setdefault("stream_options", {})["include_usage"] = True
        body = json.dumps(request_body).encode()

        self.discovery.observe(agent_id=agent_id, agent_type=agent_type, model=model, source_ip=remote_addr)

        decision = self.policy.evaluate(agent_id, model)
        if decision.action in (PolicyAction.BLOCK, PolicyAction.THROTTLE):
            status = 429 if decision.action == PolicyAction.THROTTLE else 403
            err = {"error": {"message": decision.reason, "type": "policy_violation", "rule": decision.rule_name}}
            logger.warning("POLICY %s: agent=%s reason=%s", decision.action.value.upper(), agent_id, decision.reason)

            def _err_gen():
                yield json.dumps(err).encode()
            return status, {"Content-Type": "application/json"}, _err_gen()

        fwd_headers = self._forward_headers(headers)
        url = self._resolve_upstream(agent_id, headers, path)
        t0 = time.time()

        try:
            upstream_resp = self._client.send(
                self._client.build_request(method, url, headers=fwd_headers, content=body),
                stream=True,
            )
        except httpx.HTTPError as exc:
            latency_ms = int((time.time() - t0) * 1000)
            self._emit_vitals(
                request_body=request_body,
                response_body=None,
                latency_ms=latency_ms,
                agent_id=agent_id,
                agent_type=agent_type,
                success=False,
                error_type=type(exc).__name__,
            )
            err = {"error": {"message": f"Upstream error: {exc}", "type": "proxy_error"}}

            def _err_gen2():
                yield json.dumps(err).encode()
            return 502, {"Content-Type": "application/json"}, _err_gen2()

        resp_headers = dict(upstream_resp.headers)
        chunks_collected: List[Dict[str, Any]] = []

        def _stream():
            try:
                for raw_line in upstream_resp.iter_lines():
                    yield (raw_line + "\n").encode()

                    if raw_line.startswith("data: ") and raw_line != "data: [DONE]":
                        try:
                            chunk = json.loads(raw_line[6:])
                            chunks_collected.append(chunk)
                        except (json.JSONDecodeError, ValueError):
                            pass
            finally:
                upstream_resp.close()
                latency_ms = int((time.time() - t0) * 1000)

                vitals = extract_vitals_from_stream_chunks(
                    request_body=request_body,
                    chunks=chunks_collected,
                    latency_ms=latency_ms,
                    agent_id=agent_id,
                    agent_type=agent_type,
                    success=200 <= upstream_resp.status_code < 400,
                )
                if self.on_vitals:
                    self.on_vitals(vitals)
                self.policy.record_usage(agent_id, tokens=vitals.get("token_count", 0))

                if decision.action == PolicyAction.ALERT:
                    logger.warning("POLICY ALERT: agent=%s rule=%s", agent_id, decision.rule_name)

        return upstream_resp.status_code, resp_headers, _stream()

    def _emit_vitals(self, *, request_body, response_body, latency_ms, agent_id, agent_type, success, error_type):
        vitals = extract_vitals(
            request_body=request_body,
            response_body=response_body,
            latency_ms=latency_ms,
            agent_id=agent_id,
            agent_type=agent_type,
            success=success,
            error_type=error_type,
        )
        if self.on_vitals:
            self.on_vitals(vitals)

    @staticmethod
    def _parse_body(raw: Optional[bytes]) -> Optional[Dict[str, Any]]:
        if not raw:
            return None
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return None
