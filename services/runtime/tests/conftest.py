"""Pytest configuration and global fixtures for services/runtime unit tests.

Enforces:
- Unit tests must be strictly offline.
- Outbound socket connections and DNS lookups to external (non-loopback) addresses are blocked.
- Local proxies (HTTP_PROXY, HTTPS_PROXY, ALL_PROXY) pointing to localhost cannot be used
  to bypass isolation or cause unit tests to wait on an external connection.
"""

from __future__ import annotations

import http.client
import socket
from typing import Any
import urllib.parse
import urllib.request
import pytest

ALLOWED_HOSTS = {"127.0.0.1", "localhost", "::1", "0.0.0.0", "::"}
PROXY_ENV_VARS = [
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
    "NO_PROXY",
    "no_proxy",
]


@pytest.fixture(autouse=True)
def block_outbound_network_in_unit_tests(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fixture enforcing offline network isolation for unit tests."""
    # 1. Clear all proxy environment variables to prevent urllib / httpx from routing via local proxies
    for var in PROXY_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(urllib.request, "getproxies", lambda: {})
    monkeypatch.setattr(urllib.request, "proxy_bypass", lambda host: True)

    orig_connect = socket.socket.connect
    orig_getaddrinfo = socket.getaddrinfo
    orig_create_connection = socket.create_connection
    orig_putrequest = http.client.HTTPConnection.putrequest

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

    def guarded_putrequest(
        self: http.client.HTTPConnection, method: str, url: str, *args: Any, **kwargs: Any
    ) -> None:
        # Detect proxy tunneling attempts
        tunnel_host = getattr(self, "_tunnel_host", None)
        if tunnel_host and str(tunnel_host) not in ALLOWED_HOSTS:
            raise RuntimeError(
                f"Outbound network connection blocked in unit tests (proxy tunnel): host={tunnel_host}"
            )
        # Detect absolute URLs passed to proxy
        if url.startswith("http://") or url.startswith("https://"):
            parsed = urllib.parse.urlparse(url)
            if parsed.hostname and str(parsed.hostname) not in ALLOWED_HOSTS:
                raise RuntimeError(
                    f"Outbound network connection blocked in unit tests (proxy request): host={parsed.hostname}"
                )
        # Detect direct connection host
        if self.host and str(self.host) not in ALLOWED_HOSTS:
            raise RuntimeError(
                f"Outbound network connection blocked in unit tests: host={self.host}"
            )
        return orig_putrequest(self, method, url, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(socket, "getaddrinfo", guarded_getaddrinfo)
    monkeypatch.setattr(socket, "create_connection", guarded_create_connection)
    monkeypatch.setattr(http.client.HTTPConnection, "putrequest", guarded_putrequest)
