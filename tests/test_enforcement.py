"""Tests for immune_system.enforcement — pluggable enforcement strategies."""
import asyncio
import pytest
from immune_system.enforcement import (
    CompositeEnforcement,
    EnforcementResult,
    GatewayEnforcement,
    NoOpEnforcement,
    ProcessEnforcement,
    ContainerEnforcement,
)


def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


@pytest.fixture(autouse=True)
def event_loop():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    yield loop
    loop.close()


class TestNoOpEnforcement:
    def test_block_succeeds(self, event_loop):
        e = NoOpEnforcement()
        result = event_loop.run_until_complete(e.block("a1", "test"))
        assert result.success is True
        assert result.strategy == "noop"

    def test_unblock_succeeds(self, event_loop):
        e = NoOpEnforcement()
        event_loop.run_until_complete(e.block("a1", "test"))
        result = event_loop.run_until_complete(e.unblock("a1"))
        assert result.success is True

    def test_drain_succeeds(self, event_loop):
        e = NoOpEnforcement()
        result = event_loop.run_until_complete(e.drain("a1", 0.01))
        assert result.success is True

    def test_health_check(self, event_loop):
        e = NoOpEnforcement()
        check = event_loop.run_until_complete(e.health_check("a1"))
        assert check["strategy"] == "noop"


class TestGatewayEnforcement:
    def _make_policy(self):
        """Minimal mock policy engine with add_rule/remove_rule."""
        class MockPolicy:
            def __init__(self):
                self.rules = {}
            def add_rule(self, rule):
                self.rules[rule.name] = rule
            def remove_rule(self, name):
                self.rules.pop(name, None)
        return MockPolicy()

    def test_block_injects_rule(self, event_loop):
        policy = self._make_policy()
        e = GatewayEnforcement(policy_engine=policy)
        result = event_loop.run_until_complete(e.block("agent-1", "anomaly"))
        assert result.success is True
        assert "quarantine:agent-1" in policy.rules

    def test_unblock_removes_rule(self, event_loop):
        policy = self._make_policy()
        e = GatewayEnforcement(policy_engine=policy)
        event_loop.run_until_complete(e.block("agent-1", "anomaly"))
        result = event_loop.run_until_complete(e.unblock("agent-1"))
        assert result.success is True
        assert "quarantine:agent-1" not in policy.rules

    def test_block_fails_without_policy(self, event_loop):
        e = GatewayEnforcement()
        result = event_loop.run_until_complete(e.block("a1", "test"))
        assert result.success is False

    def test_health_check_reports_blocked(self, event_loop):
        policy = self._make_policy()
        e = GatewayEnforcement(policy_engine=policy)
        event_loop.run_until_complete(e.block("a1", "test"))
        check = event_loop.run_until_complete(e.health_check("a1"))
        assert check["blocked"] is True


class TestProcessEnforcement:
    def test_block_fails_without_pid(self, event_loop):
        e = ProcessEnforcement()
        result = event_loop.run_until_complete(e.block("a1", "test"))
        assert result.success is False
        assert "pid_not_registered" in result.detail

    def test_register_and_health_check(self, event_loop):
        import os
        e = ProcessEnforcement()
        e.register_pid("a1", os.getpid())
        check = event_loop.run_until_complete(e.health_check("a1"))
        assert check["alive"] is True
        assert check["pid"] == os.getpid()

    def test_unregister(self, event_loop):
        e = ProcessEnforcement()
        e.register_pid("a1", 99999)
        e.unregister_pid("a1")
        check = event_loop.run_until_complete(e.health_check("a1"))
        assert check["registered"] is False


class TestContainerEnforcement:
    def test_block_not_registered(self, event_loop):
        e = ContainerEnforcement()
        result = event_loop.run_until_complete(e.block("a1", "test"))
        assert result.success is False

    def test_register_docker(self, event_loop):
        e = ContainerEnforcement()
        e.register_container("a1", "abc123")
        check = event_loop.run_until_complete(e.health_check("a1"))
        assert check["registered"] is True
        assert check["info"]["type"] == "docker"

    def test_register_k8s(self, event_loop):
        e = ContainerEnforcement()
        e.register_k8s("a1", "default", "my-agent")
        check = event_loop.run_until_complete(e.health_check("a1"))
        assert check["info"]["type"] == "k8s"


class TestCompositeEnforcement:
    def test_first_success_wins(self, event_loop):
        failing = NoOpEnforcement()
        failing.name = "first"

        class FailEnforcement(NoOpEnforcement):
            name = "fail"
            async def block(self, agent_id, reason):
                return EnforcementResult(False, self.name, agent_id, "block", "fail")

        comp = CompositeEnforcement([FailEnforcement(), failing])
        result = event_loop.run_until_complete(comp.block("a1", "test"))
        assert result.success is True

    def test_all_fail(self, event_loop):
        class FailEnforcement(NoOpEnforcement):
            name = "fail"
            async def block(self, agent_id, reason):
                return EnforcementResult(False, self.name, agent_id, "block", "fail")

        comp = CompositeEnforcement([FailEnforcement(), FailEnforcement()])
        result = event_loop.run_until_complete(comp.block("a1", "test"))
        assert result.success is False

    def test_health_check_aggregates(self, event_loop):
        e1 = NoOpEnforcement()
        comp = CompositeEnforcement([e1])
        check = event_loop.run_until_complete(comp.health_check("a1"))
        assert "sub_checks" in check
