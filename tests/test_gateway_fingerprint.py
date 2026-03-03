"""Tests for agent fingerprinting (deriving agent_id from request metadata)."""
import pytest

from gateway.fingerprint import AgentFingerprinter


@pytest.fixture
def fp():
    return AgentFingerprinter()


class TestExplicitHeader:
    def test_x_agent_id_header(self, fp):
        result = fp.identify(headers={"X-Agent-ID": "my-agent"}, remote_addr="1.2.3.4")
        assert result == "my-agent"

    def test_x_agent_id_stripped(self, fp):
        result = fp.identify(headers={"X-Agent-ID": "  padded  "}, remote_addr="1.2.3.4")
        assert result == "padded"

    def test_empty_x_agent_id_falls_through(self, fp):
        result = fp.identify(headers={"X-Agent-ID": ""}, remote_addr="1.2.3.4")
        assert result != ""


class TestAPIKeyFingerprint:
    def test_authorization_header(self, fp):
        result = fp.identify(headers={"Authorization": "Bearer sk-abc123"}, remote_addr="1.2.3.4")
        assert result.startswith("key-")
        assert len(result) == 16  # "key-" + 12 hex chars

    def test_x_api_key_header(self, fp):
        result = fp.identify(headers={"X-API-Key": "my-key"}, remote_addr="1.2.3.4")
        assert result.startswith("key-")

    def test_api_key_header(self, fp):
        result = fp.identify(headers={"Api-Key": "my-key"}, remote_addr="1.2.3.4")
        assert result.startswith("key-")

    def test_same_key_same_id(self, fp):
        a = fp.identify(headers={"Authorization": "Bearer sk-test"}, remote_addr="1.1.1.1")
        b = fp.identify(headers={"Authorization": "Bearer sk-test"}, remote_addr="2.2.2.2")
        assert a == b

    def test_different_key_different_id(self, fp):
        a = fp.identify(headers={"Authorization": "Bearer key-A"}, remote_addr="1.1.1.1")
        b = fp.identify(headers={"Authorization": "Bearer key-B"}, remote_addr="1.1.1.1")
        assert a != b


class TestIPFallback:
    def test_anonymous_uses_ip_and_ua(self, fp):
        result = fp.identify(headers={"User-Agent": "python-requests/2.28"}, remote_addr="10.0.0.1")
        assert result.startswith("anon-")
        assert len(result) == 17  # "anon-" + 12 hex chars

    def test_same_ip_same_ua_same_id(self, fp):
        a = fp.identify(headers={"User-Agent": "test"}, remote_addr="10.0.0.1")
        b = fp.identify(headers={"User-Agent": "test"}, remote_addr="10.0.0.1")
        assert a == b

    def test_different_ip_different_id(self, fp):
        a = fp.identify(headers={"User-Agent": "test"}, remote_addr="10.0.0.1")
        b = fp.identify(headers={"User-Agent": "test"}, remote_addr="10.0.0.2")
        assert a != b

    def test_different_ua_different_id(self, fp):
        a = fp.identify(headers={"User-Agent": "agent-A"}, remote_addr="10.0.0.1")
        b = fp.identify(headers={"User-Agent": "agent-B"}, remote_addr="10.0.0.1")
        assert a != b


class TestPriorityOrder:
    def test_explicit_beats_api_key(self, fp):
        result = fp.identify(
            headers={"X-Agent-ID": "explicit", "Authorization": "Bearer key"},
            remote_addr="1.2.3.4",
        )
        assert result == "explicit"

    def test_api_key_beats_ip(self, fp):
        result = fp.identify(
            headers={"Authorization": "Bearer key", "User-Agent": "test"},
            remote_addr="1.2.3.4",
        )
        assert result.startswith("key-")


class TestAgentType:
    def test_explicit_type_header(self, fp):
        t = fp.derive_agent_type("a1", {"X-Agent-Type": "MyCustomType"})
        assert t == "MyCustomType"

    def test_langchain_from_ua(self, fp):
        t = fp.derive_agent_type("a1", {"User-Agent": "python-langchain/0.1"})
        assert t == "LangChain"

    def test_crewai_from_ua(self, fp):
        t = fp.derive_agent_type("a1", {"User-Agent": "CrewAI-SDK/1.0"})
        assert t == "CrewAI"

    def test_default_external(self, fp):
        t = fp.derive_agent_type("a1", {"User-Agent": "curl/7.88"})
        assert t == "external"
