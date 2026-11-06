# tests/app/test_listener_tls.py
"""Each listener can terminate TLS on its own name.

The three surfaces have different audiences: the webhook receiver must be
reachable by GitHub over https, while REST and MCP are reached by the operator
and their agents. Serving them on separate names means each needs its own
certificate path -- previously only the webhook could take one, so an
agent-facing listener was necessarily plain HTTP, which is the wrong default for
a surface that carries an API key and can trigger CI.
"""

from __future__ import annotations

from rhosocial.github_actions_tracker.config import get_settings


def _service(monkeypatch, **values: str):
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear() if hasattr(get_settings, "cache_clear") else None
    return get_settings().service


class TestTheThreeListenersReadTheirOwnCertificate:
    def test_rest_has_its_own_paths(self, monkeypatch) -> None:
        service = _service(
            monkeypatch,
            GHAT_REST_SSL_CERTFILE="/certs/fullchain.pem",
            GHAT_REST_SSL_KEYFILE="/certs/privkey.pem",
        )
        assert service.rest_ssl_certfile == "/certs/fullchain.pem"
        assert service.rest_ssl_keyfile == "/certs/privkey.pem"

    def test_mcp_has_its_own_paths(self, monkeypatch) -> None:
        service = _service(
            monkeypatch,
            GHAT_MCP_SSL_CERTFILE="/certs/fullchain.pem",
            GHAT_MCP_SSL_KEYFILE="/certs/privkey.pem",
        )
        assert service.mcp_ssl_certfile == "/certs/fullchain.pem"
        assert service.mcp_ssl_keyfile == "/certs/privkey.pem"

    def test_they_default_to_plain_http(self, monkeypatch) -> None:
        """A deployment that only publishes the webhook needs no extra files."""
        service = _service(monkeypatch, GHAT_API_KEY="test-key")
        assert service.rest_ssl_certfile == ""
        assert service.mcp_ssl_certfile == ""
