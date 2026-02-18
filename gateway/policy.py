"""
Policy Engine — Evaluate and enforce rules at the gateway layer.

Policies control what agents are allowed to do:
  - Rate limits   (requests per minute, tokens per minute)
  - Model access  (whitelist of allowed models per agent pattern)
  - Token budgets (daily / per-request caps)

Actions:  ALLOW | BLOCK (HTTP 403) | THROTTLE (HTTP 429) | ALERT (allow + flag)

Policies are loaded from a list of dicts (typically sourced from a YAML/JSON
config or environment variable).
"""
import fnmatch
import json
import os
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

from immune_system.logging_config import get_logger

logger = get_logger("policy")


class PolicyAction(Enum):
    ALLOW = "allow"
    BLOCK = "block"
    THROTTLE = "throttle"
    ALERT = "alert"


@dataclass
class PolicyDecision:
    action: PolicyAction
    rule_name: str = ""
    reason: str = ""


@dataclass
class PolicyRule:
    """A single policy rule.  ``agent_pattern`` supports fnmatch globs."""
    name: str
    agent_pattern: str = "*"
    allowed_models: List[str] = field(default_factory=list)
    blocked_models: List[str] = field(default_factory=list)
    max_requests_per_minute: int = 0
    max_tokens_per_minute: int = 0
    max_tokens_per_request: int = 0
    action_on_violation: str = "block"

    @classmethod
    def from_dict(cls, d: dict) -> "PolicyRule":
        return cls(
            name=d.get("name", "unnamed"),
            agent_pattern=d.get("agent_pattern", "*"),
            allowed_models=d.get("allowed_models", []),
            blocked_models=d.get("blocked_models", []),
            max_requests_per_minute=int(d.get("max_requests_per_minute", 0)),
            max_tokens_per_minute=int(d.get("max_tokens_per_minute", 0)),
            max_tokens_per_request=int(d.get("max_tokens_per_request", 0)),
            action_on_violation=d.get("action_on_violation", "block"),
        )


class _SlidingWindowCounter:
    """Simple per-agent sliding-window counter for rate limiting."""

    def __init__(self):
        self._lock = threading.Lock()
        self._windows: Dict[str, List[float]] = {}

    def record(self, key: str, amount: float = 1.0):
        with self._lock:
            self._windows.setdefault(key, []).append((time.time(), amount))

    def count(self, key: str, window_seconds: float = 60.0) -> float:
        cutoff = time.time() - window_seconds
        with self._lock:
            entries = self._windows.get(key, [])
            entries[:] = [(t, a) for t, a in entries if t >= cutoff]
            return sum(a for _, a in entries)


class PolicyEngine:
    """Evaluate request-level policies before forwarding to upstream LLM."""

    def __init__(self, rules: Optional[List[Dict[str, Any]]] = None):
        self._rules: List[PolicyRule] = []
        self._req_counter = _SlidingWindowCounter()
        self._tok_counter = _SlidingWindowCounter()

        raw_rules = rules
        if raw_rules is None:
            env = os.getenv("GATEWAY_POLICIES", "").strip()
            if env:
                raw_rules = json.loads(env)
        if raw_rules:
            for rd in raw_rules:
                self._rules.append(PolicyRule.from_dict(rd))
            logger.info("Loaded %d policy rules", len(self._rules))

    def _matching_rules(self, agent_id: str) -> List[PolicyRule]:
        return [r for r in self._rules if fnmatch.fnmatch(agent_id, r.agent_pattern)]

    def evaluate(
        self,
        agent_id: str,
        model: str = "",
    ) -> PolicyDecision:
        """Pre-request evaluation.  Returns ALLOW when no rule is violated."""

        for rule in self._matching_rules(agent_id):
            if rule.allowed_models and model:
                if not any(fnmatch.fnmatch(model, p) for p in rule.allowed_models):
                    return PolicyDecision(
                        action=PolicyAction(rule.action_on_violation),
                        rule_name=rule.name,
                        reason=f"model '{model}' not in allowed list",
                    )

            if rule.blocked_models and model:
                if any(fnmatch.fnmatch(model, p) for p in rule.blocked_models):
                    return PolicyDecision(
                        action=PolicyAction(rule.action_on_violation),
                        rule_name=rule.name,
                        reason=f"model '{model}' is blocked",
                    )

            if rule.max_requests_per_minute > 0:
                current = self._req_counter.count(agent_id)
                if current >= rule.max_requests_per_minute:
                    return PolicyDecision(
                        action=PolicyAction.THROTTLE,
                        rule_name=rule.name,
                        reason=f"rate limit exceeded ({int(current)}/{rule.max_requests_per_minute} rpm)",
                    )

            if rule.max_tokens_per_minute > 0:
                current = self._tok_counter.count(agent_id)
                if current >= rule.max_tokens_per_minute:
                    return PolicyDecision(
                        action=PolicyAction.THROTTLE,
                        rule_name=rule.name,
                        reason=f"token budget exceeded ({int(current)}/{rule.max_tokens_per_minute} tpm)",
                    )

        return PolicyDecision(action=PolicyAction.ALLOW)

    def record_usage(self, agent_id: str, tokens: int = 0):
        """Post-response accounting."""
        self._req_counter.record(agent_id)
        if tokens > 0:
            self._tok_counter.record(agent_id, float(tokens))

    def list_rules(self) -> List[dict]:
        return [
            {
                "name": r.name,
                "agent_pattern": r.agent_pattern,
                "allowed_models": r.allowed_models,
                "blocked_models": r.blocked_models,
                "max_requests_per_minute": r.max_requests_per_minute,
                "max_tokens_per_minute": r.max_tokens_per_minute,
                "action_on_violation": r.action_on_violation,
            }
            for r in self._rules
        ]
