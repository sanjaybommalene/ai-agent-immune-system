"""Tests for gateway.routing — ProviderRegistry and RoutingTable."""

import pytest

from gateway.routing import ProviderRegistry, RoutingTable


# ── ProviderRegistry ─────────────────────────────────────────────────────


class TestProviderRegistryInit:
    def test_default_provider_always_present(self):
        reg = ProviderRegistry("https://api.openai.com")
        assert reg.default == "https://api.openai.com"
        assert reg.get("default") == "https://api.openai.com"

    def test_default_strips_trailing_slash(self):
        reg = ProviderRegistry("https://api.openai.com/")
        assert reg.default == "https://api.openai.com"

    def test_list_providers_includes_default(self):
        reg = ProviderRegistry("https://api.openai.com")
        providers = reg.list_providers()
        assert providers == {"default": "https://api.openai.com"}


class TestProviderRegistryRegister:
    def test_register_https_provider(self):
        reg = ProviderRegistry("https://api.openai.com")
        assert reg.register("azure", "https://myresource.openai.azure.com") is True
        assert reg.get("azure") == "https://myresource.openai.azure.com"

    def test_register_http_localhost(self):
        reg = ProviderRegistry("https://api.openai.com")
        assert reg.register("local", "http://localhost:8000") is True
        assert reg.get("local") == "http://localhost:8000"

    def test_register_http_127(self):
        reg = ProviderRegistry("https://api.openai.com")
        assert reg.register("local2", "http://127.0.0.1:8080") is True

    def test_reject_http_non_localhost(self):
        reg = ProviderRegistry("https://api.openai.com")
        assert reg.register("bad", "http://example.com") is False
        assert reg.get("bad") is None

    def test_reject_ftp_scheme(self):
        reg = ProviderRegistry("https://api.openai.com")
        assert reg.register("ftp", "ftp://files.example.com") is False

    def test_reject_empty_name(self):
        reg = ProviderRegistry("https://api.openai.com")
        assert reg.register("", "https://example.com") is False

    def test_reject_invalid_name_chars(self):
        reg = ProviderRegistry("https://api.openai.com")
        assert reg.register("my provider!", "https://example.com") is False
        assert reg.register("a/b", "https://example.com") is False
        assert reg.register("a b", "https://example.com") is False

    def test_valid_name_with_underscores_hyphens(self):
        reg = ProviderRegistry("https://api.openai.com")
        assert reg.register("my-provider_v2", "https://example.com") is True

    def test_strips_trailing_slash_on_register(self):
        reg = ProviderRegistry("https://api.openai.com")
        reg.register("azure", "https://azure.openai.com/")
        assert reg.get("azure") == "https://azure.openai.com"

    def test_overwrite_existing_provider(self):
        reg = ProviderRegistry("https://api.openai.com")
        reg.register("azure", "https://old.openai.azure.com")
        reg.register("azure", "https://new.openai.azure.com")
        assert reg.get("azure") == "https://new.openai.azure.com"


class TestProviderRegistryUnregister:
    def test_unregister_existing(self):
        reg = ProviderRegistry("https://api.openai.com")
        reg.register("azure", "https://azure.openai.com")
        assert reg.unregister("azure") is True
        assert reg.get("azure") is None

    def test_cannot_unregister_default(self):
        reg = ProviderRegistry("https://api.openai.com")
        assert reg.unregister("default") is False
        assert reg.get("default") == "https://api.openai.com"

    def test_unregister_nonexistent(self):
        reg = ProviderRegistry("https://api.openai.com")
        assert reg.unregister("ghost") is False


class TestProviderRegistryResolve:
    def test_resolve_by_name(self):
        reg = ProviderRegistry("https://api.openai.com")
        reg.register("azure", "https://azure.openai.com")
        assert reg.resolve("azure") == "https://azure.openai.com"

    def test_resolve_by_url(self):
        reg = ProviderRegistry("https://api.openai.com")
        reg.register("azure", "https://azure.openai.com")
        assert reg.resolve("https://azure.openai.com") == "https://azure.openai.com"

    def test_resolve_by_url_with_trailing_slash(self):
        reg = ProviderRegistry("https://api.openai.com")
        reg.register("azure", "https://azure.openai.com")
        assert reg.resolve("https://azure.openai.com/") == "https://azure.openai.com"

    def test_resolve_unknown_returns_none(self):
        reg = ProviderRegistry("https://api.openai.com")
        assert reg.resolve("unknown") is None
        assert reg.resolve("https://evil.com") is None

    def test_resolve_empty_string_returns_none(self):
        reg = ProviderRegistry("https://api.openai.com")
        assert reg.resolve("") is None
        assert reg.resolve("   ") is None

    def test_resolve_default_by_name(self):
        reg = ProviderRegistry("https://api.openai.com")
        assert reg.resolve("default") == "https://api.openai.com"


# ── RoutingTable ─────────────────────────────────────────────────────────


class TestRoutingTableSetRoute:
    def test_set_route_valid_provider(self):
        reg = ProviderRegistry("https://api.openai.com")
        reg.register("azure", "https://azure.openai.com")
        rt = RoutingTable(reg)
        assert rt.set_route("agent-1", "azure") is True

    def test_set_route_unknown_provider(self):
        reg = ProviderRegistry("https://api.openai.com")
        rt = RoutingTable(reg)
        assert rt.set_route("agent-1", "nonexistent") is False

    def test_set_route_overwrites(self):
        reg = ProviderRegistry("https://api.openai.com")
        reg.register("azure", "https://azure.openai.com")
        reg.register("vllm", "http://localhost:8000")
        rt = RoutingTable(reg)
        rt.set_route("agent-1", "azure")
        rt.set_route("agent-1", "vllm")
        assert rt.list_routes() == {"agent-1": "vllm"}


class TestRoutingTableRemoveRoute:
    def test_remove_existing_route(self):
        reg = ProviderRegistry("https://api.openai.com")
        reg.register("azure", "https://azure.openai.com")
        rt = RoutingTable(reg)
        rt.set_route("agent-1", "azure")
        assert rt.remove_route("agent-1") is True
        assert rt.list_routes() == {}

    def test_remove_nonexistent_route(self):
        reg = ProviderRegistry("https://api.openai.com")
        rt = RoutingTable(reg)
        assert rt.remove_route("ghost") is False


class TestRoutingTableResolve:
    """Three-tier resolution: header > per-agent > default."""

    def _make_table(self):
        reg = ProviderRegistry("https://api.openai.com")
        reg.register("azure", "https://azure.openai.com")
        reg.register("vllm", "http://localhost:8000")
        rt = RoutingTable(reg)
        return rt

    def test_default_when_no_header_no_route(self):
        rt = self._make_table()
        assert rt.resolve("agent-1") == "https://api.openai.com"

    def test_per_agent_route_takes_precedence_over_default(self):
        rt = self._make_table()
        rt.set_route("agent-1", "azure")
        assert rt.resolve("agent-1") == "https://azure.openai.com"

    def test_header_takes_precedence_over_per_agent(self):
        rt = self._make_table()
        rt.set_route("agent-1", "azure")
        assert rt.resolve("agent-1", header_provider="vllm") == "http://localhost:8000"

    def test_unknown_header_falls_through_to_per_agent(self):
        rt = self._make_table()
        rt.set_route("agent-1", "azure")
        assert rt.resolve("agent-1", header_provider="unknown") == "https://azure.openai.com"

    def test_unknown_header_falls_through_to_default(self):
        rt = self._make_table()
        assert rt.resolve("agent-1", header_provider="unknown") == "https://api.openai.com"

    def test_header_by_url(self):
        rt = self._make_table()
        assert rt.resolve("agent-1", header_provider="https://azure.openai.com") == "https://azure.openai.com"

    def test_empty_header_ignored(self):
        rt = self._make_table()
        rt.set_route("agent-1", "azure")
        assert rt.resolve("agent-1", header_provider="") == "https://azure.openai.com"

    def test_different_agents_different_routes(self):
        rt = self._make_table()
        rt.set_route("agent-a", "azure")
        rt.set_route("agent-b", "vllm")
        assert rt.resolve("agent-a") == "https://azure.openai.com"
        assert rt.resolve("agent-b") == "http://localhost:8000"
        assert rt.resolve("agent-c") == "https://api.openai.com"


class TestRoutingTableListRoutes:
    def test_empty_initially(self):
        reg = ProviderRegistry("https://api.openai.com")
        rt = RoutingTable(reg)
        assert rt.list_routes() == {}

    def test_reflects_current_routes(self):
        reg = ProviderRegistry("https://api.openai.com")
        reg.register("azure", "https://azure.openai.com")
        rt = RoutingTable(reg)
        rt.set_route("a1", "azure")
        rt.set_route("a2", "default")
        assert rt.list_routes() == {"a1": "azure", "a2": "default"}
