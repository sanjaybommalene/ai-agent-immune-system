"""Tests for ImmuneMemory: in-memory and with store (write_healing_event, get_failed_actions)."""
from unittest.mock import MagicMock

import pytest

from immune_system.memory import ImmuneMemory
from immune_system.diagnosis import DiagnosisType
from immune_system.healing import HealingAction


class TestImmuneMemoryInMemory:
    def test_record_healing_and_get_failed_actions(self):
        memory = ImmuneMemory(store=None)
        memory.record_healing("a1", DiagnosisType.PROMPT_DRIFT, HealingAction.RESET_MEMORY, success=False)
        memory.record_healing("a1", DiagnosisType.PROMPT_DRIFT, HealingAction.ROLLBACK_PROMPT, success=True)

        failed = memory.get_failed_actions("a1", DiagnosisType.PROMPT_DRIFT)
        assert failed == {HealingAction.RESET_MEMORY}

    def test_get_failed_actions_empty_when_no_failures(self):
        memory = ImmuneMemory(store=None)
        memory.record_healing("a1", DiagnosisType.PROMPT_INJECTION, HealingAction.REVOKE_TOOLS, success=True)
        assert memory.get_failed_actions("a1", DiagnosisType.PROMPT_INJECTION) == set()


class TestImmuneMemoryWithStore:
    """When store is set, record_healing calls store.write_healing_event; get_failed_actions uses store."""

    def test_record_healing_calls_store_write_healing_event(self):
        mock_store = MagicMock()
        memory = ImmuneMemory(store=mock_store)
        memory.record_healing(
            "agent-1",
            DiagnosisType.COST_OVERRUN,
            HealingAction.REDUCE_AUTONOMY,
            success=True,
        )
        mock_store.write_healing_event.assert_called_once()
        call_kw = mock_store.write_healing_event.call_args.kwargs
        assert call_kw["agent_id"] == "agent-1"
        assert call_kw["diagnosis_type"] == DiagnosisType.COST_OVERRUN.value
        assert call_kw["healing_action"] == HealingAction.REDUCE_AUTONOMY.value
        assert call_kw["success"] is True
        assert call_kw["validation_passed"] is True

    def test_get_failed_actions_uses_store(self):
        mock_store = MagicMock()
        mock_store.get_failed_healing_actions.return_value = ["reset_memory", "rollback_prompt"]
        memory = ImmuneMemory(store=mock_store)
        failed = memory.get_failed_actions("a1", DiagnosisType.PROMPT_DRIFT)
        mock_store.get_failed_healing_actions.assert_called_once_with("a1", DiagnosisType.PROMPT_DRIFT.value)
        assert failed == {HealingAction.RESET_MEMORY, HealingAction.ROLLBACK_PROMPT}

    def test_get_failed_actions_skips_unknown_action_string(self):
        mock_store = MagicMock()
        mock_store.get_failed_healing_actions.return_value = ["reset_memory", "unknown_action"]
        memory = ImmuneMemory(store=mock_store)
        failed = memory.get_failed_actions("a1", DiagnosisType.PROMPT_DRIFT)
        assert failed == {HealingAction.RESET_MEMORY}
