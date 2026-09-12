"""Pytest configuration and global fixtures for services/runtime unit tests.

Enforces:
- Unit tests must be strictly offline.
- Outbound socket connections and DNS lookups to external (non-loopback) addresses are blocked.
"""

from __future__ import annotations

import socket
from typing import Any
import pytest

ALLOWED_HOSTS = {"127.0.0.1", "localhost", "::1", "0.0.0.0", "::"}


@pytest.fixture(autouse=True)
def block_outbound_network_in_unit_tests(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fixture enforcing offline network isolation for unit tests."""
    orig_connect = socket.socket.connect
    orig_getaddrinfo = socket.getaddrinfo
    orig_create_connection = socket.create_connection

    def _extract_host(target: Any) -> str:
        if isinstance(target, tuple) and target:
            return str(target[0])
        return str(target)

    def guarded_connect(self: socket.socket, address: Any) -> None:
        host = _extract_host(address)
        if host not in ALLOWED_HOSTS:
            raise RuntimeError(
                f"Outbound network connection blocked in unit tests: target={address}"
            )
        return orig_connect(self, address)

    def guarded_getaddrinfo(host: Any, port: Any, *args: Any, **kwargs: Any) -> Any:
        host_str = str(host) if host is not None else ""
        if host_str and host_str not in ALLOWED_HOSTS:
            raise RuntimeError(
                f"Outbound network connection blocked in unit tests: host={host_str}"
            )
        return orig_getaddrinfo(host, port, *args, **kwargs)

    def guarded_create_connection(address: Any, *args: Any, **kwargs: Any) -> socket.socket:
        host = _extract_host(address)
        if host not in ALLOWED_HOSTS:
            raise RuntimeError(
                f"Outbound network connection blocked in unit tests: target={address}"
            )
        return orig_create_connection(address, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(socket, "getaddrinfo", guarded_getaddrinfo)
    monkeypatch.setattr(socket, "create_connection", guarded_create_connection)
