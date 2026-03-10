"""
Baseline Learning — EWMA adaptive baselines for each agent.

Uses Exponential Weighted Moving Average so baselines continuously adapt to
natural drift while still detecting sudden anomalies.  After a warmup period
(min_samples), the baseline becomes "ready" and the Sentinel uses it.

State is cached locally (CacheManager) and periodically persisted to the store
(InfluxDB or API) for durability.
"""
import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .logging_config import get_logger

logger = get_logger("baseline")

# EWMA span controls how fast the baseline adapts.  A span of 50 means
# alpha ≈ 0.039 — recent samples influence ~4% each, so a gradual shift
# takes ~50 samples to fully absorb while a sudden spike stands out.
_DEFAULT_EWMA_SPAN = 50


@dataclass
class BaselineProfile:
    """Statistical baseline for an agent (EWMA state)."""
    agent_id: str

    latency_mean: float
    latency_stddev: float
    latency_p95: float

    tokens_mean: float
    tokens_stddev: float
    tokens_p95: float

    tools_mean: float
    tools_stddev: float
    tools_p95: float

    sample_size: int

    input_tokens_mean: float = 0.0
    input_tokens_stddev: float = 0.0
    input_tokens_p95: float = 0.0
    output_tokens_mean: float = 0.0
    output_tokens_stddev: float = 0.0
    output_tokens_p95: float = 0.0

    cost_mean: float = 0.0
    cost_stddev: float = 0.0
    cost_p95: float = 0.0

    retry_rate_mean: float = 0.0
    retry_rate_stddev: float = 0.0
    error_rate_mean: float = 0.0
    error_rate_stddev: float = 0.0

    prompt_hash: str = ""

    def __str__(self):
        return (
            f"Baseline[{self.agent_id}]: "
            f"latency={self.latency_mean:.0f}ms±{self.latency_stddev:.0f}, "
            f"tokens={self.tokens_mean:.0f}±{self.tokens_stddev:.0f}, "
            f"in={self.input_tokens_mean:.0f} out={self.output_tokens_mean:.0f}, "
            f"cost=${self.cost_mean:.4f}±{self.cost_stddev:.4f}, "
            f"tools={self.tools_mean:.1f}±{self.tools_stddev:.1f}"
        )


class _EWMAMetric:
    """EWMA tracker for a single metric (mean + variance)."""
    __slots__ = ("mean", "variance", "count", "alpha", "p95_sorted")

    def __init__(self, alpha: float):
        self.mean = 0.0
        self.variance = 0.0
        self.count = 0
        self.alpha = alpha
        self.p95_sorted: List[float] = []

    @property
    def stddev(self) -> float:
        return math.sqrt(max(0.0, self.variance))

    @property
    def p95(self) -> float:
        if not self.p95_sorted:
            return self.mean
        idx = min(int(len(self.p95_sorted) * 0.95), len(self.p95_sorted) - 1)
        return self.p95_sorted[idx]

    def update(self, value: float):
        self.count += 1
        if self.count == 1:
            self.mean = value
            self.variance = 0.0
        else:
            diff = value - self.mean
            self.mean = self.alpha * value + (1.0 - self.alpha) * self.mean
            self.variance = (1.0 - self.alpha) * (self.variance + self.alpha * diff * diff)
        # Keep a bounded sorted list for p95 (last 200 values)
        self.p95_sorted.append(value)
        if len(self.p95_sorted) > 200:
            self.p95_sorted = self.p95_sorted[-200:]
        self.p95_sorted.sort()

    def to_dict(self) -> Dict:
        return {"mean": self.mean, "variance": self.variance, "count": self.count}

    def from_dict(self, d: Dict):
        self.mean = float(d.get("mean", 0.0))
        self.variance = float(d.get("variance", 0.0))
        self.count = int(d.get("count", 0))


class _AgentEWMA:
    """Collection of EWMA metrics for a single agent."""

    def __init__(self, alpha: float):
        self.latency = _EWMAMetric(alpha)
        self.tokens = _EWMAMetric(alpha)
        self.tools = _EWMAMetric(alpha)
        self.input_tokens = _EWMAMetric(alpha)
        self.output_tokens = _EWMAMetric(alpha)
        self.cost = _EWMAMetric(alpha)
        self.retry_rate = _EWMAMetric(alpha)
        self.error_rate = _EWMAMetric(alpha)
        self.prompt_hash: str = ""

    def to_dict(self) -> Dict:
        return {
            "latency": self.latency.to_dict(),
            "tokens": self.tokens.to_dict(),
            "tools": self.tools.to_dict(),
            "input_tokens": self.input_tokens.to_dict(),
            "output_tokens": self.output_tokens.to_dict(),
            "cost": self.cost.to_dict(),
            "retry_rate": self.retry_rate.to_dict(),
            "error_rate": self.error_rate.to_dict(),
            "prompt_hash": self.prompt_hash,
        }

    def from_dict(self, d: Dict):
        for key in ("latency", "tokens", "tools", "input_tokens", "output_tokens", "cost", "retry_rate", "error_rate"):
            metric: _EWMAMetric = getattr(self, key)
            metric.from_dict(d.get(key, {}))
        self.prompt_hash = d.get("prompt_hash", "")


class BaselineLearner:
    """EWMA-based baseline learner for agent telemetry."""

    def __init__(self, min_samples: int = 15, store=None, cache=None, ewma_span: int = _DEFAULT_EWMA_SPAN):
        self.min_samples = min_samples
        self.store = store
        self.cache = cache
        self.alpha = 2.0 / (ewma_span + 1)
        self._ewma: Dict[str, _AgentEWMA] = {}
        self.baselines: Dict[str, BaselineProfile] = {}
        self._pending_deceleration: Dict[str, tuple] = {}
        self._restore_from_cache()

    def _restore_from_cache(self):
        """Restore EWMA state and baselines from the local cache."""
        if not self.cache:
            return
        cached = self.cache.get_baselines()
        for agent_id, data in cached.items():
            ewma = _AgentEWMA(self.alpha)
            if "ewma" in data:
                ewma.from_dict(data["ewma"])
            self._ewma[agent_id] = ewma
            if ewma.latency.count >= self.min_samples:
                self.baselines[agent_id] = self._ewma_to_profile(agent_id, ewma)
        if self.baselines:
            logger.info("Restored %d baselines from cache", len(self.baselines))

    def _get_ewma(self, agent_id: str) -> _AgentEWMA:
        if agent_id not in self._ewma:
            self._ewma[agent_id] = _AgentEWMA(self.alpha)
        return self._ewma[agent_id]

    def update(self, agent_id: str, vitals) -> Optional[BaselineProfile]:
        """Feed a single vitals sample into the EWMA learner.

        Returns the updated BaselineProfile once warmup is complete, else None.
        """
        ewma = self._get_ewma(agent_id)
        ewma.latency.update(float(vitals.latency_ms))
        ewma.tokens.update(float(vitals.token_count))
        ewma.tools.update(float(vitals.tool_calls))
        ewma.input_tokens.update(float(getattr(vitals, "input_tokens", 0)))
        ewma.output_tokens.update(float(getattr(vitals, "output_tokens", 0)))
        ewma.cost.update(float(getattr(vitals, "cost", 0.0)))
        ewma.retry_rate.update(1.0 if vitals.retries > 0 else 0.0)
        ewma.error_rate.update(1.0 if getattr(vitals, "error_type", "") else 0.0)

        ph = getattr(vitals, "prompt_hash", "")
        if ph:
            ewma.prompt_hash = ph

        self._check_deceleration(agent_id, ewma)

        if ewma.latency.count < self.min_samples:
            return None

        profile = self._ewma_to_profile(agent_id, ewma)
        self.baselines[agent_id] = profile

        if self.cache:
            self.cache.set_baseline(agent_id, {"ewma": ewma.to_dict()})

        if ewma.latency.count == self.min_samples:
            if self.cache:
                self.cache.save_if_dirty()
            self._persist_to_store(profile)
            logger.info("Baseline ready for %s (after %d samples): %s", agent_id, self.min_samples, profile)
        elif ewma.latency.count % 100 == 0:
            self._persist_to_store(profile)

        return profile

    def _ewma_to_profile(self, agent_id: str, ewma: _AgentEWMA) -> BaselineProfile:
        return BaselineProfile(
            agent_id=agent_id,
            latency_mean=ewma.latency.mean,
            latency_stddev=ewma.latency.stddev,
            latency_p95=ewma.latency.p95,
            tokens_mean=ewma.tokens.mean,
            tokens_stddev=ewma.tokens.stddev,
            tokens_p95=ewma.tokens.p95,
            tools_mean=ewma.tools.mean,
            tools_stddev=ewma.tools.stddev,
            tools_p95=ewma.tools.p95,
            sample_size=ewma.latency.count,
            input_tokens_mean=ewma.input_tokens.mean,
            input_tokens_stddev=ewma.input_tokens.stddev,
            input_tokens_p95=ewma.input_tokens.p95,
            output_tokens_mean=ewma.output_tokens.mean,
            output_tokens_stddev=ewma.output_tokens.stddev,
            output_tokens_p95=ewma.output_tokens.p95,
            cost_mean=ewma.cost.mean,
            cost_stddev=ewma.cost.stddev,
            cost_p95=ewma.cost.p95,
            retry_rate_mean=ewma.retry_rate.mean,
            retry_rate_stddev=ewma.retry_rate.stddev,
            error_rate_mean=ewma.error_rate.mean,
            error_rate_stddev=ewma.error_rate.stddev,
            prompt_hash=ewma.prompt_hash,
        )

    def _persist_to_store(self, profile: BaselineProfile):
        if not self.store:
            return
        try:
            self.store.write_baseline_profile({
                "agent_id": profile.agent_id,
                "latency_mean": profile.latency_mean,
                "latency_stddev": profile.latency_stddev,
                "latency_p95": profile.latency_p95,
                "tokens_mean": profile.tokens_mean,
                "tokens_stddev": profile.tokens_stddev,
                "tokens_p95": profile.tokens_p95,
                "tools_mean": profile.tools_mean,
                "tools_stddev": profile.tools_stddev,
                "tools_p95": profile.tools_p95,
                "sample_size": profile.sample_size,
                "input_tokens_mean": profile.input_tokens_mean,
                "input_tokens_stddev": profile.input_tokens_stddev,
                "input_tokens_p95": profile.input_tokens_p95,
                "output_tokens_mean": profile.output_tokens_mean,
                "output_tokens_stddev": profile.output_tokens_stddev,
                "output_tokens_p95": profile.output_tokens_p95,
                "cost_mean": profile.cost_mean,
                "cost_stddev": profile.cost_stddev,
                "cost_p95": profile.cost_p95,
                "prompt_hash": profile.prompt_hash,
            })
        except Exception as exc:
            logger.warning("Failed to persist baseline to store: %s", exc)

    # ---- Compat: old orchestrator calls ----

    def learn_baseline(self, agent_id: str, vitals_list: list) -> Optional[BaselineProfile]:
        """Batch-feed vitals through EWMA (backward compat with orchestrator)."""
        profile = None
        for v in vitals_list:
            profile = self.update(agent_id, v)
        return profile

    def is_baseline_ready(self, agent_id: str, current_count: int) -> bool:
        """Check if enough samples have been collected for baseline."""
        ewma = self._ewma.get(agent_id)
        if ewma and ewma.latency.count >= self.min_samples:
            return agent_id not in self.baselines
        return current_count >= self.min_samples and agent_id not in self.baselines

    def get_baseline(self, agent_id: str) -> Optional[BaselineProfile]:
        if agent_id in self.baselines:
            return self.baselines[agent_id]
        if self.store:
            raw = self.store.get_baseline_profile(agent_id)
            if raw:
                baseline = BaselineProfile(**{k: v for k, v in raw.items() if k in BaselineProfile.__dataclass_fields__})
                self.baselines[agent_id] = baseline
                return baseline
        return None

    def has_baseline(self, agent_id: str) -> bool:
        if agent_id in self.baselines:
            return True
        if self.store:
            raw = self.store.get_baseline_profile(agent_id)
            if raw:
                self.baselines[agent_id] = BaselineProfile(**{k: v for k, v in raw.items() if k in BaselineProfile.__dataclass_fields__})
                return True
        return False

    def count_baselines(self) -> int:
        if self.baselines:
            return len(self.baselines)
        if self.store:
            return self.store.count_baselines()
        return 0

    def reset_baseline(self, agent_id: str):
        """Hard-reset: clear all EWMA state so the agent re-learns from scratch."""
        self._ewma.pop(agent_id, None)
        self.baselines.pop(agent_id, None)
        if self.cache:
            self.cache.set_baseline(agent_id, {})
        logger.info("Baseline hard-reset for %s", agent_id)

    def accelerate_learning(self, agent_id: str, ticks: int = 50):
        """Soft-reset: temporarily increase EWMA alpha so the baseline adapts
        faster for the next *ticks* samples, then reverts to normal alpha.

        This is useful after healing — the agent's "normal" may have changed
        (e.g. lower token usage after reducing autonomy) and we want the
        baseline to converge quickly.
        """
        ewma = self._ewma.get(agent_id)
        if ewma is None:
            return
        fast_alpha = min(0.3, self.alpha * 5)
        for metric_name in ("latency", "tokens", "tools", "input_tokens",
                            "output_tokens", "cost", "retry_rate", "error_rate"):
            metric: _EWMAMetric = getattr(ewma, metric_name)
            metric.alpha = fast_alpha
        self._pending_deceleration[agent_id] = (ewma.latency.count + ticks, self.alpha)
        logger.info("Baseline accelerated for %s (fast_alpha=%.3f for %d ticks)", agent_id, fast_alpha, ticks)

    def _check_deceleration(self, agent_id: str, ewma: _AgentEWMA):
        """Revert alpha after the accelerated-learning window expires."""
        entry = self._pending_deceleration.get(agent_id)
        if entry is None:
            return
        target_count, normal_alpha = entry
        if ewma.latency.count >= target_count:
            for metric_name in ("latency", "tokens", "tools", "input_tokens",
                                "output_tokens", "cost", "retry_rate", "error_rate"):
                metric: _EWMAMetric = getattr(ewma, metric_name)
                metric.alpha = normal_alpha
            del self._pending_deceleration[agent_id]
            logger.info("Baseline alpha reverted to normal for %s", agent_id)
