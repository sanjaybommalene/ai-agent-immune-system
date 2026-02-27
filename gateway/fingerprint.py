"""
Agent Fingerprinting — Derive a stable agent_id from request metadata.

Priority order:
  1. Explicit ``X-Agent-ID`` header  (cooperative / provisioned agents)
  2. Bearer token / API key hash     (unique per calling application)
  3. Client IP + User-Agent hash     (fallback for anonymous callers)
"""
import hashlib
from typing import Optional


def _hash(*parts: str) -> str:
    """SHA-256 of joined parts, truncated to 12 hex chars."""
    raw = "|".join(parts)
    return hashlib.sha256(raw.encode()).hexdigest()[:12]


class AgentFingerprinter:
    """Produces a deterministic ``agent_id`` from HTTP request metadata."""

    def identify(
        self,
        *,
        headers: dict,
        remote_addr: str = "",
    ) -> str:
        """Return a stable agent identifier for this request.

        The returned ID is safe to use as an InfluxDB tag value and as a
        display label in the dashboard.
        """

        explicit = (headers.get("X-Agent-ID") or "").strip()
        if explicit:
            return explicit

        auth = (headers.get("Authorization") or "").strip()
        api_key = (headers.get("X-API-Key") or headers.get("Api-Key") or "").strip()
        credential = auth or api_key
        if credential:
            return f"key-{_hash(credential)}"

        user_agent = (headers.get("User-Agent") or "unknown").strip()
        ip = (remote_addr or "0.0.0.0").strip()
        return f"anon-{_hash(ip, user_agent)}"

    def derive_agent_type(self, agent_id: str, headers: dict) -> str:
        """Derive an agent type label from available metadata."""
        explicit = (headers.get("X-Agent-Type") or "").strip()
        if explicit:
            return explicit

        ua = (headers.get("User-Agent") or "").lower()
        if "langchain" in ua:
            return "LangChain"
        if "crewai" in ua:
            return "CrewAI"
        if "autogen" in ua:
            return "AutoGen"
        if "llamaindex" in ua or "llama-index" in ua:
            return "LlamaIndex"
        return "external"
