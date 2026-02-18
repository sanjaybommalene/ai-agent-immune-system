"""Shared fixtures for immune system tests."""
import time
import pytest

from immune_system.cache import CacheManager
from immune_system.telemetry import AgentVitals
from immune_system.baseline import BaselineProfile


@pytest.fixture
def tmp_cache(tmp_path):
    """CacheManager backed by a temporary directory (auto-cleaned)."""
    return CacheManager(cache_dir=str(tmp_path))


@pytest.fixture
def sample_vitals():
    """Factory that produces an AgentVitals with sensible defaults.

    Call with keyword overrides, e.g. ``sample_vitals(latency_ms=500)``.
    """
    def _make(
        agent_id="agent-1",
        agent_type="test",
        latency_ms=120,
        token_count=300,
        tool_calls=2,
        retries=0,
        success=True,
        input_tokens=150,
        output_tokens=150,
        cost=0.002,
        model="gpt-4o",
        error_type="",
        prompt_hash="abc123",
        timestamp=None,
    ):
        return AgentVitals(
            timestamp=timestamp or time.time(),
            agent_id=agent_id,
            agent_type=agent_type,
            latency_ms=latency_ms,
            token_count=token_count,
            tool_calls=tool_calls,
            retries=retries,
            success=success,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost=cost,
            model=model,
            error_type=error_type,
            prompt_hash=prompt_hash,
        )
    return _make


@pytest.fixture
def learned_baseline():
    """A BaselineProfile that's past warmup with realistic values."""
    return BaselineProfile(
        agent_id="agent-1",
        latency_mean=120.0,
        latency_stddev=15.0,
        latency_p95=145.0,
        tokens_mean=300.0,
        tokens_stddev=40.0,
        tokens_p95=380.0,
        tools_mean=2.0,
        tools_stddev=0.5,
        tools_p95=3.0,
        sample_size=50,
        input_tokens_mean=150.0,
        input_tokens_stddev=20.0,
        input_tokens_p95=190.0,
        output_tokens_mean=150.0,
        output_tokens_stddev=20.0,
        output_tokens_p95=190.0,
        cost_mean=0.002,
        cost_stddev=0.0005,
        cost_p95=0.003,
        retry_rate_mean=0.05,
        retry_rate_stddev=0.04,
        error_rate_mean=0.02,
        error_rate_stddev=0.03,
        prompt_hash="abc123",
    )
