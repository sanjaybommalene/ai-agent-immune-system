"""Tests for immune_system.executor — pluggable healing executors."""
import asyncio
import pytest
from immune_system.executor import (
    ExecutionResult,
    GatewayExecutor,
    SimulatedExecutor,
    ProcessExecutor,
    ContainerExecutor,
)
from immune_system.healing import HealingAction
from immune_system.agents import AgentState, BaseAgent


def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


@pytest.fixture(autouse=True)
def event_loop():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    yield loop
    loop.close()


@pytest.fixture
def agent():
    a = BaseAgent("test-agent", "test")
    a.state = AgentState()
    return a


class TestSimulatedExecutor:
    def test_reset_memory(self, event_loop, agent):
        agent.state.memory["key"] = "value"
        ex = SimulatedExecutor()
        result = event_loop.run_until_complete(
            ex.execute("test-agent", HealingAction.RESET_MEMORY, {"agent": agent})
        )
        assert result.success is True
        assert len(agent.state.memory) == 0

    def test_rollback_prompt(self, event_loop, agent):
        agent.state.prompt_version = 3
        ex = SimulatedExecutor()
        result = event_loop.run_until_complete(
            ex.execute("test-agent", HealingAction.ROLLBACK_PROMPT, {"agent": agent})
        )
        assert result.success is True
        assert agent.state.prompt_version == 2

    def test_reduce_autonomy(self, event_loop, agent):
        ex = SimulatedExecutor()
        old_temp = agent.state.temperature
        result = event_loop.run_until_complete(
            ex.execute("test-agent", HealingAction.REDUCE_AUTONOMY, {"agent": agent})
        )
        assert result.success is True
        assert agent.state.temperature < old_temp

    def test_revoke_tools(self, event_loop, agent):
        ex = SimulatedExecutor()
        result = event_loop.run_until_complete(
            ex.execute("test-agent", HealingAction.REVOKE_TOOLS, {"agent": agent})
        )
        assert result.success is True
        assert agent.state.tools_revoked is True
        assert agent.state.max_tools == 0

    def test_reset_agent(self, event_loop, agent):
        agent.state.memory["k"] = "v"
        agent.infect("test")
        ex = SimulatedExecutor()
        result = event_loop.run_until_complete(
            ex.execute("test-agent", HealingAction.RESET_AGENT, {"agent": agent})
        )
        assert result.success is True
        assert not agent.infected

    def test_no_agent_in_context(self, event_loop):
        ex = SimulatedExecutor()
        result = event_loop.run_until_complete(
            ex.execute("test-agent", HealingAction.RESET_MEMORY, {})
        )
        assert result.success is False


class TestGatewayExecutor:
    def _make_policy(self):
        class MockPolicy:
            def __init__(self):
                self.rules = {}
            def add_rule(self, rule):
                self.rules[rule.name] = rule
            def remove_rule(self, name):
                self.rules.pop(name, None)
        return MockPolicy()

    def test_reduce_autonomy_injects_throttle(self, event_loop):
        policy = self._make_policy()
        ex = GatewayExecutor(policy_engine=policy)
        result = event_loop.run_until_complete(
            ex.execute("a1", HealingAction.REDUCE_AUTONOMY, {})
        )
        assert result.success is True
        assert "heal:throttle:a1" in policy.rules

    def test_revoke_tools_injects_block(self, event_loop):
        policy = self._make_policy()
        ex = GatewayExecutor(policy_engine=policy)
        result = event_loop.run_until_complete(
            ex.execute("a1", HealingAction.REVOKE_TOOLS, {})
        )
        assert result.success is True
        assert "heal:no-tools:a1" in policy.rules

    def test_reset_agent_injects_full_block(self, event_loop):
        policy = self._make_policy()
        ex = GatewayExecutor(policy_engine=policy)
        result = event_loop.run_until_complete(
            ex.execute("a1", HealingAction.RESET_AGENT, {})
        )
        assert result.success is True
        assert "heal:block:a1" in policy.rules

    def test_fails_without_policy(self, event_loop):
        ex = GatewayExecutor()
        result = event_loop.run_until_complete(
            ex.execute("a1", HealingAction.REDUCE_AUTONOMY, {})
        )
        assert result.success is False

    def test_reset_memory(self, event_loop):
        policy = self._make_policy()
        ex = GatewayExecutor(policy_engine=policy)
        result = event_loop.run_until_complete(
            ex.execute("a1", HealingAction.RESET_MEMORY, {})
        )
        assert result.success is True

    def test_rollback_prompt(self, event_loop):
        policy = self._make_policy()
        ex = GatewayExecutor(policy_engine=policy)
        result = event_loop.run_until_complete(
            ex.execute("a1", HealingAction.ROLLBACK_PROMPT, {})
        )
        assert result.success is True


class TestProcessExecutor:
    def test_fails_without_control_url(self, event_loop):
        ex = ProcessExecutor()
        result = event_loop.run_until_complete(
            ex.execute("a1", HealingAction.RESET_MEMORY, {})
        )
        assert result.success is False
        assert "no control URL" in result.message


class TestContainerExecutor:
    def test_fails_without_registration(self, event_loop):
        ex = ContainerExecutor()
        result = event_loop.run_until_complete(
            ex.execute("a1", HealingAction.RESET_AGENT, {})
        )
        assert result.success is False

    def test_fallback_executor(self, event_loop, agent):
        fallback = SimulatedExecutor()
        ex = ContainerExecutor(fallback=fallback)
        result = event_loop.run_until_complete(
            ex.execute("test-agent", HealingAction.RESET_MEMORY, {"agent": agent})
        )
        assert result.success is True
        assert result.executor == "simulated"
