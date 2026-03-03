"""Tests for the gateway policy engine (rate limits, model access, budgets)."""
import time

import pytest

from gateway.policy import PolicyAction, PolicyDecision, PolicyEngine, PolicyRule


@pytest.fixture
def engine_with_rules():
    rules = [
        {
            "name": "rate-limit",
            "agent_pattern": "*",
            "max_requests_per_minute": 5,
            "action_on_violation": "throttle",
        },
        {
            "name": "block-expensive",
            "agent_pattern": "anon-*",
            "blocked_models": ["gpt-4", "claude-3-opus*"],
            "action_on_violation": "block",
        },
        {
            "name": "allow-only-cheap",
            "agent_pattern": "budget-*",
            "allowed_models": ["gpt-3.5-turbo", "gpt-4o-mini"],
            "action_on_violation": "block",
        },
    ]
    return PolicyEngine(rules=rules)


class TestNoRules:
    def test_empty_engine_allows_all(self):
        engine = PolicyEngine(rules=[])
        decision = engine.evaluate("any-agent", "gpt-4")
        assert decision.action == PolicyAction.ALLOW

    def test_none_rules_allows_all(self):
        import os
        old = os.environ.get("GATEWAY_POLICIES")
        os.environ.pop("GATEWAY_POLICIES", None)
        engine = PolicyEngine(rules=None)
        assert engine.evaluate("x", "gpt-4").action == PolicyAction.ALLOW
        if old is not None:
            os.environ["GATEWAY_POLICIES"] = old


class TestModelAccess:
    def test_blocked_model_returns_block(self, engine_with_rules):
        decision = engine_with_rules.evaluate("anon-abc123", "gpt-4")
        assert decision.action == PolicyAction.BLOCK
        assert "blocked" in decision.reason

    def test_blocked_model_glob_match(self, engine_with_rules):
        decision = engine_with_rules.evaluate("anon-abc123", "claude-3-opus-20240229")
        assert decision.action == PolicyAction.BLOCK

    def test_allowed_model_passes(self, engine_with_rules):
        decision = engine_with_rules.evaluate("anon-abc123", "gpt-4o")
        assert decision.action == PolicyAction.ALLOW

    def test_allowed_list_blocks_unlisted(self, engine_with_rules):
        decision = engine_with_rules.evaluate("budget-team", "gpt-4")
        assert decision.action == PolicyAction.BLOCK
        assert "allowed" in decision.reason

    def test_allowed_list_passes_listed(self, engine_with_rules):
        decision = engine_with_rules.evaluate("budget-team", "gpt-3.5-turbo")
        assert decision.action == PolicyAction.ALLOW


class TestRateLimiting:
    def test_under_limit_allows(self, engine_with_rules):
        for _ in range(4):
            engine_with_rules.record_usage("agent-x")
        decision = engine_with_rules.evaluate("agent-x")
        assert decision.action == PolicyAction.ALLOW

    def test_over_limit_throttles(self, engine_with_rules):
        for _ in range(6):
            engine_with_rules.record_usage("agent-y")
        decision = engine_with_rules.evaluate("agent-y")
        assert decision.action == PolicyAction.THROTTLE
        assert "rate limit" in decision.reason


class TestAgentPatternMatching:
    def test_wildcard_matches_all(self, engine_with_rules):
        engine_with_rules.record_usage("literally-anything")
        assert engine_with_rules.evaluate("literally-anything").action == PolicyAction.ALLOW

    def test_anon_pattern(self, engine_with_rules):
        decision = engine_with_rules.evaluate("anon-xyz", "gpt-4")
        assert decision.action == PolicyAction.BLOCK

    def test_non_matching_pattern_skips_rule(self, engine_with_rules):
        decision = engine_with_rules.evaluate("key-abc", "gpt-4")
        assert decision.action == PolicyAction.ALLOW


class TestPolicyRuleFromDict:
    def test_minimal_dict(self):
        r = PolicyRule.from_dict({"name": "test"})
        assert r.name == "test"
        assert r.agent_pattern == "*"
        assert r.max_requests_per_minute == 0

    def test_full_dict(self):
        r = PolicyRule.from_dict({
            "name": "full",
            "agent_pattern": "key-*",
            "allowed_models": ["gpt-4o"],
            "max_requests_per_minute": 100,
            "max_tokens_per_minute": 50000,
            "action_on_violation": "alert",
        })
        assert r.agent_pattern == "key-*"
        assert r.allowed_models == ["gpt-4o"]
        assert r.max_requests_per_minute == 100
        assert r.action_on_violation == "alert"


class TestListRules:
    def test_list_rules(self, engine_with_rules):
        rules = engine_with_rules.list_rules()
        assert len(rules) == 3
        names = [r["name"] for r in rules]
        assert "rate-limit" in names
        assert "block-expensive" in names
