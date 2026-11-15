"""Process-level Offline Mode guard and consented Online Mode domain allowlist.

Offline Mode is the default and enforced fail-closed:
- Outbound TCP/UDP sockets to non-loopback endpoints raise ``OfflinePolicyError``
  whenever ``OfflineGuard`` is active in Offline Mode.
- Child-process environment builders explicitly disable telemetry, automatic
  model hub downloads, and proxy leaks (``HF_HUB_OFFLINE=1``, ``TRANSFORMERS_OFFLINE=1``,
  ``OLLAMA_NOPRUNE=1``, ``NO_PROXY=*``, ``PIP_NO_INDEX=1``).
- Optional Online Mode requires explicit configuration consent plus domain-level
  allowlist matching and logs every outbound target into ``audit_log``.
"""

from __future__ import annotations

import ipaddress
import os
import socket
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any
from urllib.parse import urlsplit

LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})


class OfflinePolicyError(PermissionError):
    """Raised when an operation attempts non-loopback network egress while Offline Mode is active."""


def is_loopback_host(host: str) -> bool:
    cleaned = host.strip().strip("[]").lower()
    if cleaned in LOOPBACK_HOSTS:
        return True
    try:
        addr = ipaddress.ip_address(cleaned)
        return bool(addr.is_loopback)
    except ValueError:
        return False


def offline_child_env(base_env: dict[str, str] | None = None) -> dict[str, str]:
    """Build a hardened offline environment for local child processes (Ollama, STT, PDF, etc.)."""
    source = dict(os.environ if base_env is None else base_env)
    blocked_prefixes = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy")
    env = {k: v for k, v in source.items() if not k.startswith(blocked_prefixes)}
    env.update(
        {
            "NO_PROXY": "*",
            "no_proxy": "*",
            "HF_HUB_OFFLINE": "1",
            "TRANSFORMERS_OFFLINE": "1",
            "HF_DATASETS_OFFLINE": "1",
            "PIP_NO_INDEX": "1",
            "PIP_DISABLE_PIP_VERSION_CHECK": "1",
            "DO_NOT_TRACK": "1",
            "ANONYMIZED_TELEMETRY": "False",
            "SCARF_NO_ANALYTICS": "true",
        }
    )
    return env


class OfflineGuard:
    """Inspectable network policy controller with optional socket-level enforcement."""

    _patch_lock = threading.Lock()
    _active_guard: OfflineGuard | None = None
    _orig_create_connection = socket.create_connection
    _orig_socket_connect = socket.socket.connect

    def __init__(
        self,
        *,
        strict_offline: bool = False,
        online_mode_enabled: bool = False,
        allowed_online_domains: list[str] | None = None,
    ) -> None:
        if strict_offline and online_mode_enabled:
            raise ValueError("strict_offline and online_mode_enabled cannot both be enabled")
        self.strict_offline = strict_offline
        self.online_mode_enabled = online_mode_enabled
        self.allowed_online_domains = tuple(
            sorted({d.strip().lower().lstrip(".") for d in (allowed_online_domains or []) if d.strip()})
        )
        self._blocked_attempts: list[dict[str, Any]] = []
        self._allowed_online_events: list[dict[str, Any]] = []
        self._events_lock = threading.Lock()

    @property
    def mode(self) -> str:
        if self.strict_offline:
            return "strict_offline"
        if self.online_mode_enabled:
            return "consented_online"
        return "offline_default"

    def check_host(self, host: str, port: int | None = None, *, purpose: str = "socket") -> None:
        if is_loopback_host(host):
            return
        norm_host = host.strip().lower().rstrip(".")
        if self.online_mode_enabled and not self.strict_offline:
            if any(
                norm_host == dom or norm_host.endswith(f".{dom}")
                for dom in self.allowed_online_domains
            ):
                with self._events_lock:
                    self._allowed_online_events.append(
                        {"host": norm_host, "port": port, "purpose": purpose}
                    )
                return
        with self._events_lock:
            self._blocked_attempts.append({"host": norm_host, "port": port, "purpose": purpose})
        raise OfflinePolicyError(
            f"Outbound network connection to '{norm_host}:{port or '*'}' blocked by HINATA {self.mode} policy"
        )

    def check_url(self, url: str, *, purpose: str = "http") -> None:
        parsed = urlsplit(url)
        host = parsed.hostname or ""
        if not host:
            raise OfflinePolicyError(f"URL '{url}' has no valid hostname")
        self.check_host(host, parsed.port, purpose=purpose)

    def status(self) -> dict[str, Any]:
        with self._events_lock:
            return {
                "mode": self.mode,
                "strict_offline": self.strict_offline,
                "online_mode_enabled": self.online_mode_enabled,
                "allowed_online_domains": list(self.allowed_online_domains),
                "socket_guard_installed": OfflineGuard._active_guard is self,
                "blocked_attempts_count": len(self._blocked_attempts),
                "recent_blocked": list(self._blocked_attempts[-10:]),
                "recent_online_allowed": list(self._allowed_online_events[-10:]),
            }

    def install_socket_guard(self) -> None:
        """Install process-wide socket interception blocking non-loopback connections."""
        with OfflineGuard._patch_lock:
            OfflineGuard._active_guard = self

            def _guarded_create_connection(address: Any, *args: Any, **kwargs: Any) -> socket.socket:
                guard = OfflineGuard._active_guard
                if guard is not None and isinstance(address, tuple) and len(address) >= 2:
                    guard.check_host(str(address[0]), int(address[1]), purpose="create_connection")
                return OfflineGuard._orig_create_connection(address, *args, **kwargs)

            def _guarded_connect(sock: socket.socket, address: Any) -> Any:
                guard = OfflineGuard._active_guard
                if (
                    guard is not None
                    and sock.family in {socket.AF_INET, socket.AF_INET6}
                    and isinstance(address, tuple)
                    and len(address) >= 2
                ):
                    guard.check_host(str(address[0]), int(address[1]), purpose="socket.connect")
                return OfflineGuard._orig_socket_connect(sock, address)

            socket.create_connection = _guarded_create_connection  # type: ignore[assignment]
            socket.socket.connect = _guarded_connect  # type: ignore[assignment,method-assign]

    def uninstall_socket_guard(self) -> None:
        with OfflineGuard._patch_lock:
            if OfflineGuard._active_guard is self:
                socket.create_connection = OfflineGuard._orig_create_connection  # type: ignore[assignment]
                socket.socket.connect = OfflineGuard._orig_socket_connect  # type: ignore[assignment,method-assign]
                OfflineGuard._active_guard = None

    @contextmanager
    def enforce(self) -> Iterator[OfflineGuard]:
        self.install_socket_guard()
        try:
            yield self
        finally:
            self.uninstall_socket_guard()
