"""
Provider Registry and Routing Table — Multi-provider upstream routing for
the LLM Gateway.

The gateway can route different agents to different LLM providers (OpenAI,
Azure OpenAI, vLLM, etc.) using a three-tier resolution chain:

    1. ``X-LLM-Provider`` request header  (agent-specified override)
    2. Per-agent routing table             (admin-configured)
    3. Default upstream                    (``LLM_UPSTREAM_URL``)

Only providers registered in the :class:`ProviderRegistry` allowlist are
accepted — unknown names or URLs are silently rejected and fall through to
the next tier, preventing SSRF.
"""
import re
import threading
from typing import Dict, Optional
from urllib.parse import urlparse

from immune_system.logging_config import get_logger

logger = get_logger("gateway.routing")

_PROVIDER_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]+$")

_SAFE_HTTP_HOSTS = frozenset({"localhost", "127.0.0.1", "0.0.0.0", "::1"})


def _validate_provider_name(name: str) -> bool:
    return bool(name) and _PROVIDER_NAME_RE.match(name) is not None


def _validate_url(url: str) -> bool:
    """Allow https:// always; allow http:// only for localhost/loopback."""
    parsed = urlparse(url)
    if parsed.scheme == "https":
        return bool(parsed.hostname)
    if parsed.scheme == "http":
        hostname = (parsed.hostname or "").lower()
        return hostname in _SAFE_HTTP_HOSTS
    return False


class ProviderRegistry:
    """Thread-safe allowlist of trusted upstream LLM providers.

    The ``"default"`` provider is always present and cannot be removed.
    """

    def __init__(self, default_upstream: str):
        self._default = default_upstream.rstrip("/")
        self._providers: Dict[str, str] = {"default": self._default}
        self._lock = threading.Lock()

    @property
    def default(self) -> str:
        return self._default

    def register(self, name: str, base_url: str) -> bool:
        """Register a named provider.  Returns False if name or URL is invalid."""
        if not _validate_provider_name(name):
            logger.warning("Invalid provider name rejected: %s", name)
            return False
        if not _validate_url(base_url):
            logger.warning("Invalid provider URL rejected: %s", base_url)
            return False
        normalized = base_url.rstrip("/")
        with self._lock:
            self._providers[name] = normalized
        logger.info("Provider registered: %s -> %s", name, normalized)
        return True

    def unregister(self, name: str) -> bool:
        """Remove a provider.  Cannot remove ``"default"``."""
        if name == "default":
            logger.warning("Cannot unregister the default provider")
            return False
        with self._lock:
            removed = self._providers.pop(name, None)
        if removed:
            logger.info("Provider unregistered: %s", name)
            return True
        return False

    def get(self, name: str) -> Optional[str]:
        """Look up a provider by name."""
        with self._lock:
            return self._providers.get(name)

    def resolve(self, name_or_url: str) -> Optional[str]:
        """Resolve a provider name *or* raw URL to a registered base URL.

        Returns ``None`` when the value does not match any registered
        provider — the caller should fall through to the next tier.
        """
        stripped = name_or_url.strip()
        if not stripped:
            return None
        with self._lock:
            if stripped in self._providers:
                return self._providers[stripped]
            normalized = stripped.rstrip("/")
            for url in self._providers.values():
                if url == normalized:
                    return url
        return None

    def list_providers(self) -> Dict[str, str]:
        with self._lock:
            return dict(self._providers)


class RoutingTable:
    """Per-agent routing: ``agent_id -> provider_name``.

    Resolution order (first match wins):
        1. ``header_provider`` argument  (from ``X-LLM-Provider`` header)
        2. Per-agent route
        3. Registry default
    """

    def __init__(self, registry: ProviderRegistry):
        self.registry = registry
        self._routes: Dict[str, str] = {}
        self._lock = threading.Lock()

    def set_route(self, agent_id: str, provider: str) -> bool:
        """Assign *agent_id* to *provider*.  Fails if provider is unknown."""
        if self.registry.get(provider) is None:
            logger.warning(
                "Cannot route agent %s to unknown provider %s",
                agent_id, provider,
            )
            return False
        with self._lock:
            self._routes[agent_id] = provider
        logger.info("Route set: %s -> %s", agent_id, provider)
        return True

    def remove_route(self, agent_id: str) -> bool:
        with self._lock:
            removed = self._routes.pop(agent_id, None)
        if removed:
            logger.info("Route removed: %s (was -> %s)", agent_id, removed)
            return True
        return False

    def resolve(self, agent_id: str, header_provider: str = "") -> str:
        """Resolve the upstream base URL for *agent_id*.

        Three-tier chain: header -> per-agent table -> default.
        """
        if header_provider:
            url = self.registry.resolve(header_provider)
            if url is not None:
                return url

        with self._lock:
            provider_name = self._routes.get(agent_id)
        if provider_name:
            url = self.registry.get(provider_name)
            if url is not None:
                return url

        return self.registry.default

    def list_routes(self) -> Dict[str, str]:
        with self._lock:
            return dict(self._routes)
