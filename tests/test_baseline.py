"""Tests for BaselineLearner and EWMA convergence."""
import pytest

from immune_system.baseline import BaselineLearner, BaselineProfile
from immune_system.cache import CacheManager


class TestEWMAWarmup:
    def test_no_baseline_before_min_samples(self, sample_vitals):
        bl = BaselineLearner(min_samples=5)
        for _ in range(4):
            result = bl.update("a1", sample_vitals())
        assert result is None
        assert not bl.has_baseline("a1")

    def test_baseline_ready_at_min_samples(self, sample_vitals):
        bl = BaselineLearner(min_samples=5)
        for _ in range(5):
            result = bl.update("a1", sample_vitals())
        assert result is not None
        assert isinstance(result, BaselineProfile)
        assert bl.has_baseline("a1")

    def test_baseline_updates_continuously(self, sample_vitals):
        bl = BaselineLearner(min_samples=5)
        for _ in range(10):
            bl.update("a1", sample_vitals())
        p1 = bl.get_baseline("a1")
        for _ in range(10):
            bl.update("a1", sample_vitals(latency_ms=200))
        p2 = bl.get_baseline("a1")
        assert p2.latency_mean > p1.latency_mean


class TestEWMAConvergence:
    def test_converges_to_constant_value(self, sample_vitals):
        bl = BaselineLearner(min_samples=5, ewma_span=10)
        for _ in range(100):
            bl.update("a1", sample_vitals(latency_ms=100, token_count=200, tool_calls=3))
        baseline = bl.get_baseline("a1")
        assert abs(baseline.latency_mean - 100) < 1.0
        assert abs(baseline.tokens_mean - 200) < 1.0
        assert abs(baseline.tools_mean - 3.0) < 0.1

    def test_adapts_to_drift(self, sample_vitals):
        bl = BaselineLearner(min_samples=5, ewma_span=10)
        for _ in range(50):
            bl.update("a1", sample_vitals(latency_ms=100))
        baseline_before = bl.get_baseline("a1")

        for _ in range(50):
            bl.update("a1", sample_vitals(latency_ms=200))
        baseline_after = bl.get_baseline("a1")

        assert baseline_after.latency_mean > baseline_before.latency_mean
        assert abs(baseline_after.latency_mean - 200) < 5.0


class TestStddevFloor:
    def test_constant_metric_has_zero_variance(self, sample_vitals):
        bl = BaselineLearner(min_samples=5, ewma_span=10)
        for _ in range(20):
            bl.update("a1", sample_vitals(latency_ms=100))
        baseline = bl.get_baseline("a1")
        assert baseline.latency_stddev < 1.0


class TestCacheRoundTrip:
    def test_restore_from_cache(self, tmp_path, sample_vitals):
        cache = CacheManager(cache_dir=str(tmp_path))
        cache.load()
        bl = BaselineLearner(min_samples=5, cache=cache)
        for _ in range(10):
            bl.update("a1", sample_vitals())
        cache.save()
        original_baseline = bl.get_baseline("a1")

        cache2 = CacheManager(cache_dir=str(tmp_path))
        cache2.load()
        bl2 = BaselineLearner(min_samples=5, cache=cache2)
        restored_baseline = bl2.get_baseline("a1")

        assert restored_baseline is not None
        assert abs(restored_baseline.latency_mean - original_baseline.latency_mean) < 0.01

    def test_flushes_on_first_baseline(self, tmp_path, sample_vitals):
        cache = CacheManager(cache_dir=str(tmp_path))
        cache.load()
        bl = BaselineLearner(min_samples=5, cache=cache)
        for i in range(5):
            bl.update("a1", sample_vitals())
        # After min_samples, cache.save_if_dirty() is called in baseline.py
        # Verify the cache file was written
        cache_file = tmp_path / "state.json"
        assert cache_file.exists()


class TestPromptHash:
    def test_tracks_prompt_hash(self, sample_vitals):
        bl = BaselineLearner(min_samples=5)
        for _ in range(10):
            bl.update("a1", sample_vitals(prompt_hash="hash-v1"))
        baseline = bl.get_baseline("a1")
        assert baseline.prompt_hash == "hash-v1"


class TestMultipleAgents:
    def test_independent_baselines(self, sample_vitals):
        bl = BaselineLearner(min_samples=5)
        for _ in range(10):
            bl.update("a1", sample_vitals(agent_id="a1", latency_ms=100))
            bl.update("a2", sample_vitals(agent_id="a2", latency_ms=500))
        b1 = bl.get_baseline("a1")
        b2 = bl.get_baseline("a2")
        assert b1 is not None
        assert b2 is not None
        assert abs(b1.latency_mean - 100) < 5.0
        assert abs(b2.latency_mean - 500) < 5.0


class TestBackwardCompat:
    def test_learn_baseline_batch(self, sample_vitals):
        bl = BaselineLearner(min_samples=5)
        vitals = [sample_vitals() for _ in range(10)]
        result = bl.learn_baseline("a1", vitals)
        assert result is not None
        assert bl.has_baseline("a1")

    def test_count_baselines(self, sample_vitals):
        bl = BaselineLearner(min_samples=5)
        for _ in range(10):
            bl.update("a1", sample_vitals())
        assert bl.count_baselines() == 1
