"""Tests for CacheManager: persistence, schema versioning, atomic writes, API key."""
import json
import os
import stat
import threading

import pytest

from immune_system.cache import CacheManager, _SCHEMA_VERSION


class TestLoadSaveRoundTrip:
    def test_fresh_start_no_file(self, tmp_path):
        cm = CacheManager(cache_dir=str(tmp_path))
        state = cm.load()
        assert state["run_id"] is None
        assert state["baselines"] == {}
        assert state["quarantine"] == []
        assert state["_schema_version"] == _SCHEMA_VERSION

    def test_save_and_reload(self, tmp_path):
        cm = CacheManager(cache_dir=str(tmp_path))
        cm.load()
        cm.set_run_id("run-abc")
        cm.set_baseline("a1", {"ewma": {"latency": {"mean": 100}}})
        cm.add_quarantine("a2")
        cm.save()

        cm2 = CacheManager(cache_dir=str(tmp_path))
        cm2.load()
        assert cm2.get_run_id() == "run-abc"
        assert cm2.get_baseline("a1") == {"ewma": {"latency": {"mean": 100}}}
        assert cm2.get_quarantine() == ["a2"]

    def test_file_permissions_restricted(self, tmp_path):
        cm = CacheManager(cache_dir=str(tmp_path))
        cm.load()
        cm.set_run_id("x")
        cm.save()
        mode = os.stat(cm._cache_path).st_mode
        assert mode & 0o777 == stat.S_IRUSR | stat.S_IWUSR  # 0600


class TestCorruptFile:
    def test_corrupt_json_starts_fresh(self, tmp_path):
        cache_path = tmp_path / "state.json"
        cache_path.write_text("{invalid json!!!")
        cm = CacheManager(cache_dir=str(tmp_path))
        state = cm.load()
        assert state["run_id"] is None

    def test_non_dict_json_starts_fresh(self, tmp_path):
        cache_path = tmp_path / "state.json"
        cache_path.write_text('"just a string"')
        cm = CacheManager(cache_dir=str(tmp_path))
        state = cm.load()
        assert state["run_id"] is None


class TestSchemaVersion:
    def test_matching_version_loads_data(self, tmp_path):
        data = {"_schema_version": _SCHEMA_VERSION, "run_id": "run-ok", "baselines": {}, "quarantine": [], "api_key": None}
        (tmp_path / "state.json").write_text(json.dumps(data))
        cm = CacheManager(cache_dir=str(tmp_path))
        cm.load()
        assert cm.get_run_id() == "run-ok"

    def test_old_version_discards_cache(self, tmp_path):
        data = {"_schema_version": 0, "run_id": "run-old", "baselines": {}, "quarantine": []}
        (tmp_path / "state.json").write_text(json.dumps(data))
        cm = CacheManager(cache_dir=str(tmp_path))
        cm.load()
        # Should have discarded and generated a new run_id
        assert cm._state["run_id"] is None

    def test_missing_version_treated_as_zero(self, tmp_path):
        data = {"run_id": "run-legacy", "baselines": {}}
        (tmp_path / "state.json").write_text(json.dumps(data))
        cm = CacheManager(cache_dir=str(tmp_path))
        cm.load()
        assert cm._state["run_id"] is None


class TestAtomicWrite:
    def test_no_partial_files_on_success(self, tmp_path):
        cm = CacheManager(cache_dir=str(tmp_path))
        cm.load()
        cm.set_run_id("test")
        cm.save()
        files = list(tmp_path.iterdir())
        assert len(files) == 1
        assert files[0].name == "state.json"

    def test_creates_directory_if_missing(self, tmp_path):
        nested = tmp_path / "deep" / "nested"
        cm = CacheManager(cache_dir=str(nested))
        cm.load()
        cm.set_run_id("test")
        cm.save()
        assert (nested / "state.json").exists()


class TestConcurrentAccess:
    def test_concurrent_writes_no_crash(self, tmp_path):
        cm = CacheManager(cache_dir=str(tmp_path))
        cm.load()
        errors = []

        def writer(i):
            try:
                cm.add_quarantine(f"agent-{i}")
                cm.save()
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=writer, args=(i,)) for i in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == []
        # All agents should be in quarantine (set semantics — no duplicates)
        q = cm.get_quarantine()
        assert len(q) == 20


class TestEnvVarOverride:
    def test_immune_cache_dir_env(self, tmp_path, monkeypatch):
        custom_dir = str(tmp_path / "custom")
        monkeypatch.setenv("IMMUNE_CACHE_DIR", custom_dir)
        # Re-import to pick up new env var for default
        from immune_system import cache as cache_mod
        orig = cache_mod._DEFAULT_CACHE_DIR
        try:
            cache_mod._DEFAULT_CACHE_DIR = os.environ.get(
                "IMMUNE_CACHE_DIR",
                os.path.join(str(os.path.expanduser("~")), ".immune_cache"),
            )
            cm = CacheManager()
            assert cm._cache_dir == custom_dir
        finally:
            cache_mod._DEFAULT_CACHE_DIR = orig


class TestAPIKey:
    def test_auto_generates_key(self, tmp_cache):
        tmp_cache.load()
        key = tmp_cache.get_api_key()
        assert key.startswith("imm-")
        assert len(key) > 10

    def test_cached_key_persists(self, tmp_path):
        cm = CacheManager(cache_dir=str(tmp_path))
        cm.load()
        key1 = cm.get_api_key()
        cm.save()

        cm2 = CacheManager(cache_dir=str(tmp_path))
        cm2.load()
        assert cm2.get_api_key() == key1

    def test_env_var_takes_precedence(self, tmp_path, monkeypatch):
        cm = CacheManager(cache_dir=str(tmp_path))
        cm.load()
        _ = cm.get_api_key()  # generates one
        monkeypatch.setenv("INGEST_API_KEY", "env-key-123")
        assert cm.get_api_key() == "env-key-123"


class TestRunId:
    def test_auto_generates(self, tmp_cache):
        tmp_cache.load()
        rid = tmp_cache.get_run_id()
        assert rid.startswith("run-")

    def test_set_and_get(self, tmp_cache):
        tmp_cache.load()
        tmp_cache.set_run_id("run-custom")
        assert tmp_cache.get_run_id() == "run-custom"

    def test_idempotent(self, tmp_cache):
        tmp_cache.load()
        r1 = tmp_cache.get_run_id()
        r2 = tmp_cache.get_run_id()
        assert r1 == r2


class TestQuarantine:
    def test_add_remove(self, tmp_cache):
        tmp_cache.load()
        tmp_cache.add_quarantine("a1")
        tmp_cache.add_quarantine("a2")
        assert set(tmp_cache.get_quarantine()) == {"a1", "a2"}
        tmp_cache.remove_quarantine("a1")
        assert tmp_cache.get_quarantine() == ["a2"]

    def test_no_duplicates(self, tmp_cache):
        tmp_cache.load()
        tmp_cache.add_quarantine("a1")
        tmp_cache.add_quarantine("a1")
        assert tmp_cache.get_quarantine() == ["a1"]

    def test_remove_nonexistent_noop(self, tmp_cache):
        tmp_cache.load()
        tmp_cache.remove_quarantine("nope")
        assert tmp_cache.get_quarantine() == []


class TestSaveIfDirty:
    def test_saves_when_dirty(self, tmp_path):
        cm = CacheManager(cache_dir=str(tmp_path))
        cm.load()
        cm.set_run_id("dirty-test")
        assert cm._dirty
        cm.save_if_dirty()
        assert not cm._dirty
        assert (tmp_path / "state.json").exists()

    def test_skips_when_clean(self, tmp_path):
        cm = CacheManager(cache_dir=str(tmp_path))
        cm.load()
        cm.save_if_dirty()
        assert not (tmp_path / "state.json").exists()
