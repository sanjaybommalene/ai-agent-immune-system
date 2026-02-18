"""
Immune System SDK - Lightweight reporter for real AI agents.

Usage:
    from immune_sdk import ImmuneReporter

    reporter = ImmuneReporter(agent_id="my-agent", base_url="http://localhost:8090")

    # After each LLM call:
    reporter.report(
        input_tokens=response.usage.prompt_tokens,
        output_tokens=response.usage.completion_tokens,
        latency_ms=450,
        tool_calls=2,
        model="gpt-4o",
        success=True,
    )
"""
import time
from typing import Optional

try:
    import requests as _requests
except ImportError:
    _requests = None


class ImmuneReporter:
    """Reports agent vitals to the Immune System via its HTTP ingest API."""

    def __init__(
        self,
        agent_id: str,
        base_url: str = "http://localhost:8090",
        agent_type: str = "external",
        model: str = "",
        timeout: float = 5.0,
    ):
        if _requests is None:
            raise RuntimeError("immune_sdk requires 'requests'. Install with: pip install requests")
        self.agent_id = agent_id
        self.agent_type = agent_type
        self.model = model
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._registered = False

    def _register(self):
        """Register the agent with the immune system (idempotent)."""
        if self._registered:
            return
        try:
            _requests.post(
                f"{self._base_url}/api/v1/agents/register",
                json={
                    "agent_id": self.agent_id,
                    "agent_type": self.agent_type,
                    "model": self.model,
                },
                timeout=self._timeout,
            )
            self._registered = True
        except Exception:
            pass

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
        """Report a single execution's vitals to the immune system.

        Args:
            input_tokens: Prompt / input tokens from the LLM response.
            output_tokens: Completion / output tokens from the LLM response.
            latency_ms: Wall-clock latency of the agent turn in milliseconds.
            tool_calls: Number of tool/function calls made during this turn.
            retries: Number of retry attempts for this execution.
            success: Whether the agent completed its task successfully.
            cost: Estimated cost in USD for this execution.
            model: LLM model used (defaults to the model set at init).
            error_type: Error category if the execution failed (e.g. "rate_limit", "timeout").
            prompt_hash: Hash of the system prompt to detect prompt drift.
        """
        self._register()
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
            _requests.post(
                f"{self._base_url}/api/v1/ingest",
                json=payload,
                timeout=self._timeout,
            )
        except Exception:
            pass
