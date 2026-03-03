"""
Vitals Extractor — Parse LLM request/response pairs into AgentVitals dicts.

Supports the OpenAI Chat Completions format (used by OpenAI, Azure OpenAI,
and many open-source providers that expose an OpenAI-compatible API).
"""
import hashlib
import time
from typing import Any, Dict, List, Optional

COST_PER_1K_TOKENS: Dict[str, Dict[str, float]] = {
    "gpt-4": {"input": 0.03, "output": 0.06},
    "gpt-4-turbo": {"input": 0.01, "output": 0.03},
    "gpt-4o": {"input": 0.005, "output": 0.015},
    "gpt-4o-mini": {"input": 0.00015, "output": 0.0006},
    "gpt-3.5-turbo": {"input": 0.0005, "output": 0.0015},
    "o1": {"input": 0.015, "output": 0.06},
    "o1-mini": {"input": 0.003, "output": 0.012},
    "o3-mini": {"input": 0.0011, "output": 0.0044},
    "claude-3-opus": {"input": 0.015, "output": 0.075},
    "claude-3-sonnet": {"input": 0.003, "output": 0.015},
    "claude-3-haiku": {"input": 0.00025, "output": 0.00125},
    "claude-3.5-sonnet": {"input": 0.003, "output": 0.015},
    "claude-4-sonnet": {"input": 0.003, "output": 0.015},
    "claude-4-opus": {"input": 0.015, "output": 0.075},
    "gemini-2.0-flash": {"input": 0.0001, "output": 0.0004},
    "gemini-2.0-pro": {"input": 0.00125, "output": 0.005},
}

_DEFAULT_COST = {"input": 0.005, "output": 0.015}


def _match_cost(model: str) -> Dict[str, float]:
    """Best-effort model name matching against the cost table."""
    lower = model.lower()
    for key, cost in COST_PER_1K_TOKENS.items():
        if key in lower:
            return cost
    return _DEFAULT_COST


def _estimate_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    rates = _match_cost(model)
    return round(
        input_tokens * rates["input"] / 1000.0
        + output_tokens * rates["output"] / 1000.0,
        6,
    )


def _extract_system_prompt(messages: List[Dict[str, Any]]) -> str:
    """Return the concatenated content of all system/developer messages."""
    parts = []
    for msg in messages:
        role = msg.get("role", "")
        if role in ("system", "developer"):
            content = msg.get("content", "")
            if isinstance(content, str):
                parts.append(content)
            elif isinstance(content, list):
                parts.extend(
                    p.get("text", "") for p in content if isinstance(p, dict)
                )
    return "\n".join(parts)


def _count_tool_calls(response_body: Dict[str, Any]) -> int:
    """Count tool/function calls across all choices in the response."""
    count = 0
    for choice in response_body.get("choices", []):
        msg = choice.get("message", {})
        tool_calls = msg.get("tool_calls") or msg.get("function_call")
        if isinstance(tool_calls, list):
            count += len(tool_calls)
        elif tool_calls:
            count += 1
    return count


def extract_vitals(
    *,
    request_body: Dict[str, Any],
    response_body: Optional[Dict[str, Any]],
    latency_ms: int,
    agent_id: str,
    agent_type: str = "external",
    success: bool = True,
    error_type: str = "",
) -> Dict[str, Any]:
    """Build an AgentVitals-compatible dict from an OpenAI-format request/response pair."""

    model = ""
    if response_body:
        model = response_body.get("model", "")
    if not model:
        model = request_body.get("model", "unknown")

    usage = (response_body or {}).get("usage", {})
    input_tokens = int(usage.get("prompt_tokens", 0))
    output_tokens = int(usage.get("completion_tokens", 0))
    token_count = input_tokens + output_tokens

    tool_calls = _count_tool_calls(response_body) if response_body else 0

    system_prompt = _extract_system_prompt(request_body.get("messages", []))
    prompt_hash = (
        hashlib.sha256(system_prompt.encode()).hexdigest()[:16]
        if system_prompt
        else ""
    )

    cost = _estimate_cost(model, input_tokens, output_tokens)

    return {
        "agent_id": agent_id,
        "agent_type": agent_type,
        "latency_ms": latency_ms,
        "token_count": token_count,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "tool_calls": tool_calls,
        "retries": 0,
        "success": success,
        "cost": cost,
        "model": model,
        "error_type": error_type,
        "prompt_hash": prompt_hash,
        "timestamp": time.time(),
    }


def extract_vitals_from_stream_chunks(
    *,
    request_body: Dict[str, Any],
    chunks: List[Dict[str, Any]],
    latency_ms: int,
    agent_id: str,
    agent_type: str = "external",
    success: bool = True,
    error_type: str = "",
) -> Dict[str, Any]:
    """Build vitals from accumulated streaming chunks.

    The last chunk with ``stream_options.include_usage`` often carries a
    ``usage`` field.  If not, we fall back to counting content tokens from
    the delta payloads (rough approximation).
    """
    model = request_body.get("model", "unknown")
    usage: Dict[str, int] = {}
    tool_call_count = 0

    for chunk in chunks:
        if not isinstance(chunk, dict):
            continue
        if chunk.get("model"):
            model = chunk["model"]
        if chunk.get("usage"):
            usage = chunk["usage"]
        for choice in chunk.get("choices", []):
            delta = choice.get("delta", {})
            tc = delta.get("tool_calls")
            if isinstance(tc, list):
                tool_call_count += sum(
                    1 for t in tc if t.get("index") is not None and t.get("id")
                )

    input_tokens = int(usage.get("prompt_tokens", 0))
    output_tokens = int(usage.get("completion_tokens", 0))
    if not usage:
        content_chars = sum(
            len(c.get("delta", {}).get("content", ""))
            for ch in chunks
            if isinstance(ch, dict)
            for c in ch.get("choices", [])
        )
        output_tokens = max(1, content_chars // 4)

    token_count = input_tokens + output_tokens

    system_prompt = _extract_system_prompt(request_body.get("messages", []))
    prompt_hash = (
        hashlib.sha256(system_prompt.encode()).hexdigest()[:16]
        if system_prompt
        else ""
    )

    cost = _estimate_cost(model, input_tokens, output_tokens)

    return {
        "agent_id": agent_id,
        "agent_type": agent_type,
        "latency_ms": latency_ms,
        "token_count": token_count,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "tool_calls": tool_call_count,
        "retries": 0,
        "success": success,
        "cost": cost,
        "model": model,
        "error_type": error_type,
        "prompt_hash": prompt_hash,
        "timestamp": time.time(),
    }
