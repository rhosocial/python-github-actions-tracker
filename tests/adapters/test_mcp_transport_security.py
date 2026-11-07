# tests/adapters/test_mcp_transport_security.py
"""A public MCP deployment must be reachable by its own name.

The SDK answers ``421 Invalid Host header`` for any ``Host`` outside its
allowlist, and that allowlist is loopback unless someone fills it. A deployment
published under a public hostname therefore fails on every request with a status
that says nothing about the cause -- the client sees "connection error" and the
operator sees a healthy ``/readyz``. These tests pin the derivation so that
setting ``GHAT_RESOURCE_URL`` is enough.
"""

from __future__ import annotations

import pytest

from rhosocial.github_actions_tracker.adapters.mcp.server import _transport_security


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in (
        "GHAT_MCP_ALLOWED_HOSTS",
        "GHAT_MCP_ALLOWED_ORIGINS",
        "GHAT_RESOURCE_URL",
    ):
        monkeypatch.delenv(name, raising=False)
    yield


class TestTheAllowlistIsDerivedFromThePublishedName:
    def test_public_name_gets_a_wildcard_port(self, monkeypatch) -> None:
        monkeypatch.setenv("GHAT_RESOURCE_URL", "https://tracker-mcp.example.com:8001/mcp")
        settings = _transport_security("https://tracker-mcp.example.com:8001/mcp")
        assert settings is not None
        assert "tracker-mcp.example.com:*" in settings.allowed_hosts
        assert "https://tracker-mcp.example.com:8001" in settings.allowed_origins

    def test_the_host_a_client_actually_sends_is_allowed(self, monkeypatch) -> None:
        """``Host`` carries the port, and the wildcard is what matches it."""
        name = "github-actions-tracker-mcp.n.rho.im"
        settings = _transport_security(f"https://{name}:8001/mcp")
        assert settings is not None
        host = f"{name}:8001"
        assert host in settings.allowed_hosts or any(
            host.startswith(allowed[:-2] + ":")
            for allowed in settings.allowed_hosts
            if allowed.endswith(":*")
        )

    def test_loopback_survives(self) -> None:
        settings = _transport_security("https://tracker-mcp.example.com:8001/mcp")
        assert settings is not None
        for host in ("localhost:*", "127.0.0.1:*", "[::1]:*"):
            assert host in settings.allowed_hosts

    def test_no_public_name_leaves_the_library_default(self, monkeypatch) -> None:
        """Not overriding means the loopback default stays, not a narrower one."""
        assert _transport_security("") is None
        monkeypatch.setenv("GHAT_RESOURCE_URL", "")
        assert _transport_security("") is None

    def test_explicit_override_wins(self, monkeypatch) -> None:
        monkeypatch.setenv("GHAT_MCP_ALLOWED_HOSTS", "edge.example.com,other.example.com")
        monkeypatch.setenv("GHAT_MCP_ALLOWED_ORIGINS", "https://edge.example.com")
        settings = _transport_security("https://ignored.example.com:8001/mcp")
        assert settings is not None
        assert "edge.example.com" in settings.allowed_hosts
        assert "other.example.com" in settings.allowed_hosts
        assert "ignored.example.com:*" not in settings.allowed_hosts
        assert (
            settings.allowed_origins
            == [
                "https://edge.example.com",
                "https://ignored.example.com:8001",
            ]
            or settings.allowed_origins[0] == "https://edge.example.com"
        )

    def test_entries_are_deduplicated(self, monkeypatch) -> None:
        monkeypatch.setenv("GHAT_MCP_ALLOWED_HOSTS", "localhost:*,a.example.com")
        settings = _transport_security("")
        assert settings is not None
        assert settings.allowed_hosts.count("localhost:*") == 1
