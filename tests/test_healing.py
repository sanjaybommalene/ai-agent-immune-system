"""Tests for Healer, healing policies, and action selection."""
import pytest
from unittest.mock import MagicMock

from immune_system.healing import (
    Healer,
    HealingAction,
    HealingResult,
    HEALING_POLICIES,
)
from immune_system.diagnosis import DiagnosisType


class TestHealingPolicies:
    def test_all_diagnosis_types_have_policies(self):
        for dt in DiagnosisType:
            assert dt in HEALING_POLICIES, f"Missing policy for {dt}"

    def test_every_policy_ends_with_reset_agent(self):
        for dt, actions in HEALING_POLICIES.items():
            assert actions[-1] == HealingAction.RESET_AGENT, (
                f"Policy for {dt} does not end with RESET_AGENT"
            )

    def test_no_duplicate_actions_in_policy(self):
        for dt, actions in HEALING_POLICIES.items():
            assert len(actions) == len(set(actions)), f"Duplicates in {dt} policy"

    def test_cost_overrun_policy_exists(self):
        policy = HEALING_POLICIES[DiagnosisType.COST_OVERRUN]
        assert HealingAction.REDUCE_AUTONOMY in policy
        assert HealingAction.RESET_AGENT in policy


class TestPolicyLadder:
    @pytest.fixture
    def healer(self):
        return Healer(telemetry_collector=None, baseline_learner=None, sentinel=None)

    def test_get_policy_known_type(self, healer):
        policy = healer.get_healing_policy(DiagnosisType.PROMPT_DRIFT)
        assert len(policy) > 0
        assert all(isinstance(a, HealingAction) for a in policy)

    def test_get_policy_unknown_falls_back(self, healer):
        policy = healer.get_healing_policy(DiagnosisType.UNKNOWN)
        assert len(policy) > 0

    def test_next_action_skips_failed(self, healer):
        failed = {HealingAction.RESET_MEMORY, HealingAction.ROLLBACK_PROMPT}
        action = healer.get_next_action(DiagnosisType.PROMPT_DRIFT, failed)
        assert action not in failed
        assert action is not None

    def test_next_action_returns_none_when_exhausted(self, healer):
        all_actions = set(HEALING_POLICIES[DiagnosisType.MEMORY_CORRUPTION])
        action = healer.get_next_action(DiagnosisType.MEMORY_CORRUPTION, all_actions)
        assert action is None

    def test_next_action_returns_first_when_no_failures(self, healer):
        policy = HEALING_POLICIES[DiagnosisType.PROMPT_INJECTION]
        action = healer.get_next_action(DiagnosisType.PROMPT_INJECTION, set())
        assert action == policy[0]


class TestHealingActions:
    def test_all_actions_have_values(self):
        for action in HealingAction:
            assert isinstance(action.value, str)
            assert len(action.value) > 0

    def test_reset_agent_exists(self):
        assert HealingAction.RESET_AGENT.value == "reset_agent"

    def test_no_clone_agent(self):
        values = {a.value for a in HealingAction}
        assert "clone_agent" not in values


class TestHealingResult:
    def test_result_fields(self):
        result = HealingResult(
            agent_id="a1",
            action=HealingAction.RESET_MEMORY,
            success=True,
            validation_passed=True,
            message="Memory cleared",
        )
        assert result.agent_id == "a1"
        assert result.success
        assert result.validation_passed


class TestHealerExecution:
    """Real-world: healer.apply_healing calls agent.state methods and returns HealingResult."""

    @pytest.fixture
    def mock_agent(self):
        agent = MagicMock()
        agent.agent_id = "a1"
        agent.state = MagicMock()
        agent.state.prompt_version = 1
        agent.state.temperature = 0.7
        agent.state.max_tools = 10
        agent.cure = MagicMock()
        return agent

    @pytest.fixture
    def mock_baseline_learner(self):
        """Healer validates by calling baseline_learner.get_baseline(); return None to skip validation."""
        bl = MagicMock()
        bl.get_baseline.return_value = None
        return bl

    @pytest.mark.asyncio
    async def test_apply_healing_reset_memory_calls_agent(self, mock_agent, mock_baseline_learner):
        healer = Healer(telemetry_collector=MagicMock(), baseline_learner=mock_baseline_learner, sentinel=MagicMock())
        result = await healer.apply_healing(mock_agent, HealingAction.RESET_MEMORY)
        mock_agent.state.reset_memory.assert_called_once()
        assert result.success
        assert result.agent_id == "a1"
        assert result.action == HealingAction.RESET_MEMORY

    @pytest.mark.asyncio
    async def test_apply_healing_reset_agent_calls_cure(self, mock_agent, mock_baseline_learner):
        healer = Healer(telemetry_collector=MagicMock(), baseline_learner=mock_baseline_learner, sentinel=MagicMock())
        await healer.apply_healing(mock_agent, HealingAction.RESET_AGENT)
        # Healer calls agent.cure() in RESET_AGENT branch and again in common path
        assert mock_agent.cure.call_count >= 1

    @pytest.mark.asyncio
    async def test_apply_healing_revoke_tools_calls_agent(self, mock_agent, mock_baseline_learner):
        healer = Healer(telemetry_collector=MagicMock(), baseline_learner=mock_baseline_learner, sentinel=MagicMock())
        result = await healer.apply_healing(mock_agent, HealingAction.REVOKE_TOOLS)
        mock_agent.state.revoke_tools.assert_called_once()
        assert result.success
