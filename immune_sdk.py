"""
Immune System SDK — Lightweight reporter for real AI agents.

Reports are buffered locally and flushed in a background thread to avoid
blocking the agent's hot path.  Supports API key authentication.

Usage:
    from immune_sdk import ImmuneReporter

    reporter = ImmuneReporter(
        agent_id="my-agent",
        base_url="http://localhost:8090",
        api_key="imm-...",
    )

    # After each LLM call:
    reporter.report(
        input_tokens=response.usage.prompt_tokens,
        output_tokens=response.usage.completion_tokens,
        latency_ms=450,
        tool_calls=2,
        model="gpt-4o",
        success=True,
    )

    # On shutdown:
    reporter.close()
"""
import atexit
import logging
import queue
import threading
import time
from typing import Callable, Optional

try:
    import requests as _requests
except ImportError:
    _requests = None

_log = logging.getLogger("immune_sdk")

_BUFFER_MAX = 100
_FLUSH_INTERVAL = 1.0  # seconds
_FLUSH_BATCH = 20


class ImmuneReporter:
    """Reports agent vitals to the Immune System via its HTTP ingest API."""

    def __init__(
        self,
        agent_id: str,
        base_url: str = "http://localhost:8090",
        agent_type: str = "external",
        model: str = "",
        api_key: str = "",
        timeout: float = 5.0,
        on_error: Optional[Callable] = None,
    ):
        if _requests is None:
            raise RuntimeError("immune_sdk requires 'requests'. Install with: pip install requests")
        self.agent_id = agent_id
        self.agent_type = agent_type
        self.model = model
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._timeout = timeout
        self._on_error = on_error
        self._registered = False
        self._closed = False
        self._queue: queue.Queue = queue.Queue(maxsize=_BUFFER_MAX)
        self._thread = threading.Thread(target=self._flush_loop, daemon=True)
        self._thread.start()
        atexit.register(self.close)

    def _headers(self):
        h = {"Content-Type": "application/json"}
        if self._api_key:
            h["X-API-KEY"] = self._api_key
        return h

    def _register(self):
        if self._registered:
            return
        try:
            _requests.post(
                f"{self._base_url}/api/v1/agents/register",
                json={"agent_id": self.agent_id, "agent_type": self.agent_type, "model": self.model},
                headers=self._headers(),
                timeout=self._timeout,
            )
            self._registered = True
        except Exception as exc:
            self._handle_error(exc)

    def report(
        self,
        input_tokens: int = 0,
        output_tokens: int = 0,
        latency_ms: int = 0,
        tool_calls: int = 0,
        retries: int = 0,
        success: bool = True,
        cost: float = 0.0,
        model: Optional[str] = None,
        error_type: str = "",
        prompt_hash: str = "",
    ):
        """Buffer a vitals report for async submission."""
        if self._closed:
            return
        payload = {
            "agent_id": self.agent_id,
            "agent_type": self.agent_type,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "token_count": input_tokens + output_tokens,
            "latency_ms": latency_ms,
            "tool_calls": tool_calls,
            "retries": retries,
            "success": success,
            "cost": cost,
            "model": model or self.model,
            "error_type": error_type,
            "prompt_hash": prompt_hash,
            "timestamp": time.time(),
        }
        try:
            self._queue.put_nowait(payload)
        except queue.Full:
            _log.warning("SDK buffer full — dropping report for %s", self.agent_id)

    def flush(self):
        """Send all buffered reports immediately (blocking)."""
        self._register()
        batch = []
        while not self._queue.empty():
            try:
                batch.append(self._queue.get_nowait())
            except queue.Empty:
                break
        for payload in batch:
            self._send(payload)

    def close(self):
        """Flush remaining reports and stop the background thread."""
        if self._closed:
            return
        self._closed = True
        self.flush()

    def _flush_loop(self):
        while not self._closed:
            batch = []
            try:
                first = self._queue.get(timeout=_FLUSH_INTERVAL)
                batch.append(first)
            except queue.Empty:
                continue

            while len(batch) < _FLUSH_BATCH:
                try:
                    batch.append(self._queue.get_nowait())
                except queue.Empty:
                    break

            self._register()
            for payload in batch:
                self._send(payload)

    def _send(self, payload: dict):
        try:
            _requests.post(
                f"{self._base_url}/api/v1/ingest",
                json=payload,
                headers=self._headers(),
                timeout=self._timeout,
            )
        except Exception as exc:
            self._handle_error(exc)

    def _handle_error(self, exc: Exception):
        if self._on_error:
            self._on_error(exc)
        else:
            _log.debug("SDK error: %s", exc)
