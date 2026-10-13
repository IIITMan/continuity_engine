"""Replay-only network guard.

The guard blocks outbound/non-loopback network access while allowing local IPC.
This distinction is required on Windows: asyncio's ProactorEventLoop may implement
``socket.socketpair()`` using a TCP connection to 127.0.0.1. Treating every TCP
connect as external breaks normal event-loop construction inside replay mode.

This process-level guard is defense-in-depth, not a security sandbox. Production-grade
replay isolation should additionally use an OS/container egress rule; a Python
monkeypatch cannot defend against native extensions or code that deliberately bypasses
these APIs.
"""
from __future__ import annotations

import ipaddress
import socket
from functools import wraps
from threading import RLock
from typing import Any, Callable


class ReplayNetworkAccessError(RuntimeError):
    """Raised when replay-mode code attempts non-local network access."""


class ReplayNetworkGuard:
    # This monkeypatch is process-wide. Reference counting prevents nested/concurrent
    # replay engines from restoring APIs while another replay guard is still active.
    _lock = RLock()
    _active_count = 0
    _originals: list[tuple[Any, str, Any]] = []
    _LOCALHOST_NAMES = {"localhost", "localhost.localdomain"}

    def __init__(self):
        self._entered = False

    @classmethod
    def _host_is_loopback(cls, host: Any) -> bool:
        if isinstance(host, bytes):
            try:
                host = host.decode("ascii")
            except UnicodeDecodeError:
                return False
        if not isinstance(host, str):
            return False
        normalized = host.rstrip(".").casefold()
        if normalized in cls._LOCALHOST_NAMES:
            return True
        try:
            return ipaddress.ip_address(normalized).is_loopback
        except ValueError:
            return False

    @classmethod
    def _is_local_endpoint(cls, address: Any, family: int | None = None) -> bool:
        # Unix-domain sockets are local IPC, not external networking.
        if family == getattr(socket, "AF_UNIX", object()):
            return True
        # AF_UNIX addresses are commonly filesystem paths rather than tuples.
        if isinstance(address, (str, bytes)) and family == getattr(socket, "AF_UNIX", None):
            return True
        # TCP/UDP IPv4 and IPv6 endpoints are tuples: (host, port[, flowinfo, scope]).
        return isinstance(address, tuple) and bool(address) and cls._host_is_loopback(address[0])

    @classmethod
    def _deny(cls, detail: str = "non-loopback network access") -> ReplayNetworkAccessError:
        return ReplayNetworkAccessError(
            f"{detail} is forbidden during Continuity Engine replay; local loopback/IPC is allowed"
        )

    @classmethod
    def _wrap_connect(cls, original: Callable[..., Any]) -> Callable[..., Any]:
        @wraps(original)
        def guarded(sock: socket.socket, address: Any, *args: Any, **kwargs: Any) -> Any:
            if cls._is_local_endpoint(address, getattr(sock, "family", None)):
                return original(sock, address, *args, **kwargs)
            raise cls._deny("non-loopback socket connection")

        return guarded

    @classmethod
    def _wrap_create_connection(cls, original: Callable[..., Any]) -> Callable[..., Any]:
        @wraps(original)
        def guarded(address: Any, *args: Any, **kwargs: Any) -> Any:
            if cls._is_local_endpoint(address):
                return original(address, *args, **kwargs)
            raise cls._deny("non-loopback socket.create_connection")

        return guarded

    @classmethod
    def _wrap_getaddrinfo(cls, original: Callable[..., Any]) -> Callable[..., Any]:
        @wraps(original)
        def guarded(host: Any, port: Any, *args: Any, **kwargs: Any) -> Any:
            # None is used by local bind/server setup and does not request a remote host.
            # All named hosts must be literal loopback addresses or localhost, so replay
            # cannot perform arbitrary external DNS resolution through this API.
            if host is None or host == "":
                return original(host, port, *args, **kwargs)
            if not cls._host_is_loopback(host):
                raise cls._deny("external name resolution")
            return original(host, port, *args, **kwargs)

        return guarded

    @classmethod
    def _wrap_sendto(cls, original: Callable[..., Any]) -> Callable[..., Any]:
        @wraps(original)
        def guarded(sock: socket.socket, data: Any, *args: Any, **kwargs: Any) -> Any:
            if getattr(sock, "family", None) == getattr(socket, "AF_UNIX", object()):
                return original(sock, data, *args, **kwargs)
            address = kwargs.get("address")
            if address is None and args:
                # socket.sendto supports sendto(data, address) and sendto(data, flags, address).
                address = args[-1]
            if cls._is_local_endpoint(address, getattr(sock, "family", None)):
                return original(sock, data, *args, **kwargs)
            raise cls._deny("non-loopback datagram send")

        return guarded

    @classmethod
    def _wrap_connected_send(cls, original: Callable[..., Any]) -> Callable[..., Any]:
        @wraps(original)
        def guarded(sock: socket.socket, *args: Any, **kwargs: Any) -> Any:
            family = getattr(sock, "family", None)
            if family == getattr(socket, "AF_UNIX", object()):
                return original(sock, *args, **kwargs)
            try:
                peer = sock.getpeername()
            except OSError:
                # Let the original operation raise the correct error for an unconnected socket.
                return original(sock, *args, **kwargs)
            if cls._is_local_endpoint(peer, family):
                return original(sock, *args, **kwargs)
            raise cls._deny("send on an already-connected non-loopback socket")

        return guarded

    def __enter__(self) -> "ReplayNetworkGuard":
        with self._lock:
            if self._entered:
                return self
            if self.__class__._active_count == 0:
                cls = self.__class__
                patch_specs: list[tuple[Any, str, Callable[..., Any]]] = [
                    (socket, "create_connection", cls._wrap_create_connection),
                    (socket, "getaddrinfo", cls._wrap_getaddrinfo),
                    (socket.socket, "connect", cls._wrap_connect),
                    (socket.socket, "connect_ex", cls._wrap_connect),
                    (socket.socket, "sendto", cls._wrap_sendto),
                    (socket.socket, "send", cls._wrap_connected_send),
                    (socket.socket, "sendall", cls._wrap_connected_send),
                ]
                if hasattr(socket.socket, "sendmsg"):
                    patch_specs.append((socket.socket, "sendmsg", cls._wrap_connected_send))

                originals: list[tuple[Any, str, Any]] = []
                for obj, name, wrapper_factory in patch_specs:
                    if hasattr(obj, name):
                        original = getattr(obj, name)
                        originals.append((obj, name, original))
                        setattr(obj, name, wrapper_factory(original))
                cls._originals = originals
            self.__class__._active_count += 1
            self._entered = True
            return self

    def __exit__(self, exc_type, exc, tb) -> None:
        with self._lock:
            if not self._entered:
                return
            self.__class__._active_count = max(0, self.__class__._active_count - 1)
            self._entered = False
            if self.__class__._active_count == 0:
                for obj, name, original in reversed(self.__class__._originals):
                    setattr(obj, name, original)
                self.__class__._originals.clear()
