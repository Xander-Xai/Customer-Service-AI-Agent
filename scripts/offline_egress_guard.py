"""Egress guard for the one-command offline demo (Issue #120).

Loaded as a pytest plugin (``-p offline_egress_guard``). Any attempt to make a
real network request during the offline demo fails the run loudly instead of
silently degrading to a fallback. Proves the "no API key / no real model"
promise structurally rather than by inspecting return values.

Only loopback (``localhost`` / ``127.0.0.1`` / ``::1``) and UNIX sockets are
allowed, so local in-process infrastructure that needs a loopback dial is not
misclassified as egress.
"""

from __future__ import annotations

import socket
from collections.abc import Callable
from typing import Any

_LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1", ""}

_EGRESS_MESSAGE = (
    "OFFLINE demo attempted unauthorized network egress; "
    "make demo-offline must run with provider=local and no external calls."
)


def _is_loopback(address: Any) -> bool:
    host = None
    if isinstance(address, (tuple, list)) and address:
        host = address[0]
    elif isinstance(address, str):
        host = address
    return host in _LOOPBACK_HOSTS


def _blocked(*_args: Any, **_kwargs: Any) -> Any:
    raise AssertionError(_EGRESS_MESSAGE)


def install_egress_guard() -> Callable[[], None]:
    """Make any non-loopback HTTP/socket egress raise immediately.

    Returns a ``restore()`` callable so tests can install the guard without
    leaking patched globals into the rest of the pytest session. The pytest
    plugin keeps it installed for the whole demo session by ignoring the return.
    """
    import httpx

    original_httpx_async_send = httpx.AsyncClient.send
    original_httpx_sync_send = httpx.Client.send
    original_connect = socket.socket.connect
    original_getaddrinfo = socket.getaddrinfo

    httpx.AsyncClient.send = _blocked  # type: ignore[method-assign]
    httpx.Client.send = _blocked  # type: ignore[method-assign]

    def _guarded_connect(self: socket.socket, address: Any) -> Any:
        if _is_loopback(address):
            return original_connect(self, address)
        raise AssertionError(f"{_EGRESS_MESSAGE} (blocked connect to {address!r})")

    socket.socket.connect = _guarded_connect  # type: ignore[method-assign]

    def _guarded_getaddrinfo(host: Any, port: Any, *args: Any, **kwargs: Any) -> Any:
        if _is_loopback(host):
            return original_getaddrinfo(host, port, *args, **kwargs)
        raise AssertionError(f"{_EGRESS_MESSAGE} (blocked DNS lookup for {host!r})")

    socket.getaddrinfo = _guarded_getaddrinfo  # type: ignore[assignment]

    def _restore() -> None:
        httpx.AsyncClient.send = original_httpx_async_send  # type: ignore[method-assign]
        httpx.Client.send = original_httpx_sync_send  # type: ignore[method-assign]
        socket.socket.connect = original_connect  # type: ignore[method-assign]
        socket.getaddrinfo = original_getaddrinfo  # type: ignore[assignment]

    return _restore


def pytest_configure(config: Any) -> None:
    install_egress_guard()


__all__ = ["install_egress_guard", "pytest_configure"]
