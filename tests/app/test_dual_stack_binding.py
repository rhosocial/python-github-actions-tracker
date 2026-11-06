# tests/app/test_dual_stack_binding.py
"""``::`` must mean both families, or an IPv4 client gets "connection refused".

A listener configured with the IPv6 wildcard reads as "every address"; on Linux
that is normally dual-stack, but ``IPV6_V6ONLY`` defaults to 1 in some container
namespaces and uvicorn never touches the flag. The result is a server that
answers on IPv6 only -- and an IPv4 client sees a refused connection, which is
indistinguishable from a service that is down.

Seen once: the deployment bound the webhook receiver to ``::`` on a NAS whose
container namespace kept the flag on. IPv6 clients (and GitHub) worked; the
operator's own machine, reaching it over a different network, could not.
"""

from __future__ import annotations

import socket

import pytest

from rhosocial.github_actions_tracker.adapters.cli.commands.serve import _dual_stack_socket


class TestTheWildcardBindsBothFamilies:
    def test_ipv4_reaches_a_socket_bound_to_the_wildcard(self) -> None:
        sock = _dual_stack_socket("::", 0)
        try:
            sock.listen(1)
            port = sock.getsockname()[1]
            client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            client.settimeout(3)
            try:
                client.connect(("127.0.0.1", port))
            finally:
                client.close()
        finally:
            sock.close()

    def test_it_really_is_an_ipv6_socket(self) -> None:
        sock = _dual_stack_socket("::", 0)
        try:
            assert sock.family == socket.AF_INET6
            assert sock.getsockname()[1] > 0, "the kernel picked a port"
        finally:
            sock.close()

    @pytest.mark.parametrize("host", ["0.0.0.0", "127.0.0.1"])
    def test_the_helper_is_only_for_the_wildcard(self, host: str) -> None:
        """An IPv4 host is uvicorn's job; the helper would bind the wrong family."""
        with pytest.raises(OSError):
            _dual_stack_socket(host, 0)
