"""Pytest configuration and global fixtures for services/runtime unit tests.

Enforces:
- Unit tests must be strictly offline.
- Outbound socket connections to external (non-loopback) addresses are blocked.
"""

from __future__ import annotations

import socket
import pytest

ALLOWED_HOSTS = {"127.0.0.1", "localhost", "::1"}


@pytest.fixture(autouse=True)
def block_outbound_network_in_unit_tests(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fixture enforcing offline network isolation for unit tests."""
    orig_connect = socket.socket.connect

    def guarded_connect(self: socket.socket, address: tuple[str, int] | Any) -> None:
        host = address[0] if isinstance(address, tuple) and address else address
        if host not in ALLOWED_HOSTS:
            raise RuntimeError(
                f"Outbound network connection blocked in unit tests: target={address}"
            )
        return orig_connect(self, address)

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
