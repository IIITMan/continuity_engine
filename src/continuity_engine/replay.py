"""Replay-only network guard.

This process-level guard blocks Python socket connection APIs while a replay engine is
active. Production-grade isolation should additionally use an OS/container egress rule;
no in-process monkeypatch can defend against arbitrary native extensions.
"""
from __future__ import annotations
import socket
from threading import RLock
from typing import Any


class ReplayNetworkAccessError(RuntimeError):
    pass


class ReplayNetworkGuard:
    # The monkeypatch is process-wide. Reference counting keeps nested/concurrent replay
    # engines from accidentally restoring network APIs while another replay is active.
    _lock = RLock()
    _active_count = 0
    _originals: list[tuple[Any, str, Any]] = []

    def __init__(self):
        self._entered = False

    @staticmethod
    def _blocked(*args: Any, **kwargs: Any) -> Any:
        raise ReplayNetworkAccessError("network access is forbidden during Continuity Engine replay")

    def __enter__(self) -> "ReplayNetworkGuard":
        with self._lock:
            if self._entered:
                return self
            if self.__class__._active_count == 0:
                # Guard both the common convenience API and socket methods used by clients.
                originals: list[tuple[Any, str, Any]] = []
                for obj, name in (
                    (socket, "create_connection"),
                    (socket.socket, "connect"),
                    (socket.socket, "connect_ex"),
                ):
                    if hasattr(obj, name):
                        originals.append((obj, name, getattr(obj, name)))
                        setattr(obj, name, self._blocked)
                self.__class__._originals = originals
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
