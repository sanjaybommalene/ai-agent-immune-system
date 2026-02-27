"""
CacheManager — local state cache for restart resilience and fast lookups.

Maintains a JSON snapshot of critical runtime state on disk. Solves restart
amnesia (baselines, quarantine, run_id survive restarts) and avoids InfluxDB
round-trips on hot paths (sentinel checks).

Thread-safe: Flask threads and the asyncio event loop may access concurrently.
"""
import json
import os
import stat
import tempfile
import threading
import asyncio
from pathlib import Path
from typing import Any, Dict, List, Optional
from uuid import uuid4

from .logging_config import get_logger

logger = get_logger("cache")

_DEFAULT_CACHE_DIR = os.environ.get(
    "IMMUNE_CACHE_DIR",
    os.path.join(str(Path.home()), ".immune_cache"),
)
_DEFAULT_FLUSH_INTERVAL = 30  # seconds
_SCHEMA_VERSION = 1


class CacheManager:
    """Atomic JSON file cache with periodic async flushing."""

    def __init__(self, cache_dir: Optional[str] = None, flush_interval: float = _DEFAULT_FLUSH_INTERVAL):
        self._cache_dir = cache_dir or _DEFAULT_CACHE_DIR
        self._cache_path = os.path.join(self._cache_dir, "state.json")
        self._flush_interval = flush_interval
        self._lock = threading.Lock()
        self._dirty = False
        self._state: Dict[str, Any] = self._empty_state()
        self._flush_task: Optional[asyncio.Task] = None

    @staticmethod
    def _empty_state() -> Dict[str, Any]:
        return {
            "_schema_version": _SCHEMA_VERSION,
            "run_id": None,
            "baselines": {},
            "quarantine": [],
            "api_key": None,
        }

    def load(self) -> Dict[str, Any]:
        """Load state from disk. Returns empty defaults if file missing or corrupt."""
        if not os.path.exists(self._cache_path):
            logger.info("No cache file found at %s — starting fresh", self._cache_path)
            return self._state
        try:
            with open(self._cache_path, "r") as fh:
                data = json.load(fh)
            stored_version = data.get("_schema_version", 0)
            if stored_version != _SCHEMA_VERSION:
                logger.warning(
                    "Cache schema version mismatch (file=%s, expected=%s) — discarding stale cache",
                    stored_version, _SCHEMA_VERSION,
                )
                return self._state
            merged = self._empty_state()
            merged.update(data)
            self._state = merged
            logger.info("Cache loaded from %s (run_id=%s, baselines=%d)",
                        self._cache_path, self._state.get("run_id"), len(self._state.get("baselines", {})))
        except Exception as exc:
            logger.warning("Failed to load cache (%s) — starting fresh: %s", self._cache_path, exc)
        return self._state

    def save(self):
        """Atomic write: temp file + rename to prevent corruption."""
        os.makedirs(self._cache_dir, exist_ok=True)
        with self._lock:
            snapshot = json.dumps(self._state, default=str, indent=2)
            self._dirty = False
        fd, tmp_path = tempfile.mkstemp(dir=self._cache_dir, suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as fh:
                fh.write(snapshot)
            os.replace(tmp_path, self._cache_path)
            os.chmod(self._cache_path, stat.S_IRUSR | stat.S_IWUSR)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    def _mark_dirty(self):
        self._dirty = True

    def save_if_dirty(self):
        """Flush to disk immediately if there are unsaved changes."""
        if self._dirty:
            self.save()

    # ---- run_id ----

    def get_run_id(self) -> str:
        """Return persisted run_id, generating one if absent."""
        with self._lock:
            rid = self._state.get("run_id")
            if not rid:
                rid = f"run-{uuid4().hex[:12]}"
                self._state["run_id"] = rid
                self._mark_dirty()
            return rid

    def set_run_id(self, run_id: str):
        with self._lock:
            self._state["run_id"] = run_id
            self._mark_dirty()

    # ---- baselines (EWMA state) ----

    def get_baselines(self) -> Dict[str, Dict[str, Any]]:
        with self._lock:
            return dict(self._state.get("baselines", {}))

    def set_baseline(self, agent_id: str, profile: Dict[str, Any]):
        with self._lock:
            self._state.setdefault("baselines", {})[agent_id] = profile
            self._mark_dirty()

    def get_baseline(self, agent_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            return self._state.get("baselines", {}).get(agent_id)

    # ---- quarantine ----

    def get_quarantine(self) -> List[str]:
        with self._lock:
            return list(self._state.get("quarantine", []))

    def set_quarantine(self, agent_ids: List[str]):
        with self._lock:
            self._state["quarantine"] = list(agent_ids)
            self._mark_dirty()

    def add_quarantine(self, agent_id: str):
        with self._lock:
            q = self._state.setdefault("quarantine", [])
            if agent_id not in q:
                q.append(agent_id)
                self._mark_dirty()

    def remove_quarantine(self, agent_id: str):
        with self._lock:
            q = self._state.get("quarantine", [])
            if agent_id in q:
                q.remove(agent_id)
                self._mark_dirty()

    # ---- API key ----

    def get_api_key(self) -> str:
        """Return API key: env var INGEST_API_KEY takes precedence, then
        cached value, then auto-generate and persist for dev convenience."""
        env_key = os.environ.get("INGEST_API_KEY")
        if env_key:
            return env_key
        with self._lock:
            key = self._state.get("api_key")
            if not key:
                key = f"imm-{uuid4().hex}"
                self._state["api_key"] = key
                self._mark_dirty()
            return key

    # ---- periodic flush ----

    async def start_periodic_flush(self):
        """Run as an asyncio task — flushes dirty state to disk periodically."""
        while True:
            await asyncio.sleep(self._flush_interval)
            if self._dirty:
                try:
                    self.save()
                    logger.debug("Cache flushed to disk")
                except Exception as exc:
                    logger.warning("Cache flush failed: %s", exc)

    def start_flush_task(self, loop: Optional[asyncio.AbstractEventLoop] = None):
        """Launch the periodic flush as a background asyncio task."""
        _loop = loop or asyncio.get_event_loop()
        self._flush_task = _loop.create_task(self.start_periodic_flush())

    def shutdown(self):
        """Flush immediately and cancel periodic task."""
        if self._flush_task and not self._flush_task.done():
            self._flush_task.cancel()
        if self._dirty:
            try:
                self.save()
            except Exception as exc:
                logger.warning("Final cache flush failed: %s", exc)
