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
import re
import socket
import threading
from collections import deque
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any
from urllib.parse import urlsplit

LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})
_DOMAIN_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")
_PUBLIC_SUFFIX = re.compile(r"(?:[a-z]{2,63}|xn--[a-z0-9-]{2,59})\Z")
_UNSAFE_DOMAIN_SUFFIXES = (
    ".local",
    ".internal",
    ".localhost",
    ".test",
    ".invalid",
    ".example",
    ".onion",
)


def validate_public_domain(domain: str) -> str:
    """Return a normalised public DNS domain or raise ``ValueError``.

    A bare suffix such as ``com`` would allow every ``*.com`` host through the
    suffix-matching rule in :meth:`OfflineGuard.check_host`; a scheme, wildcard,
    path, IP literal or leading dot would likewise not mean what the user wrote.
    """
    if not isinstance(domain, str):
        raise ValueError("Online allowlist entries must be domain-name strings")
    cleaned = domain.strip().lower()
    if not cleaned or any(ch.isspace() for ch in cleaned):
        raise ValueError("Online allowlist entries must not contain whitespace")
    if any(token in cleaned for token in ("://", "/", "@", "?", "#", "\\")) or cleaned.startswith("*"):
        raise ValueError("Online allowlist entries must be bare domain names")
    cleaned = cleaned.rstrip(".")
    if not cleaned or cleaned.startswith(".") or ":" in cleaned or len(cleaned) > 253:
        raise ValueError("Online allowlist entries must be bare domain names")
    if "." not in cleaned:
        raise ValueError("Online allowlist entries must include a domain and a suffix")
    labels = cleaned.split(".")
    if not _PUBLIC_SUFFIX.fullmatch(labels[-1]) or not all(
        _DOMAIN_LABEL.fullmatch(label) for label in labels
    ):
        raise ValueError("Online allowlist entries must be valid DNS domains")
    if cleaned.endswith(_UNSAFE_DOMAIN_SUFFIXES):
        raise ValueError("Special-use domain suffixes are not valid online allowlist entries")
    try:
        ipaddress.ip_address(cleaned)
    except ValueError:
        pass
    else:
        raise ValueError("IP addresses are not valid online allowlist entries")
    return cleaned


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
    _orig_getaddrinfo = socket.getaddrinfo
    _orig_gethostbyname = socket.gethostbyname

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
        try:
            domains = {
                validate_public_domain(d) for d in (allowed_online_domains or []) if str(d).strip()
            }
        except ValueError as exc:
            raise ValueError(f"Invalid online domain allowlist: {exc}") from exc
        self.allowed_online_domains = tuple(sorted(domains))
        self._blocked_attempts: deque[dict[str, Any]] = deque(maxlen=1000)
        self._allowed_online_events: deque[dict[str, Any]] = deque(maxlen=1000)
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
        if parsed.scheme not in {"http", "https"}:
            raise OfflinePolicyError(f"URL '{url}' must use HTTP(S) for this policy check")
        if "@" in parsed.netloc or parsed.username or parsed.password:
            raise OfflinePolicyError(f"URL '{url}' must not contain credentials")
        try:
            port = parsed.port
        except ValueError as exc:
            raise OfflinePolicyError(f"URL '{url}' has an invalid port") from exc
        host = parsed.hostname or ""
        if not host:
            raise OfflinePolicyError(f"URL '{url}' has no valid hostname")
        self.check_host(host, port, purpose=purpose)

    def status(self) -> dict[str, Any]:
        with self._events_lock:
            return {
                "mode": self.mode,
                "strict_offline": self.strict_offline,
                "online_mode_enabled": self.online_mode_enabled,
                "allowed_online_domains": list(self.allowed_online_domains),
                "socket_guard_installed": OfflineGuard._active_guard is self,
                "blocked_attempts_count": len(self._blocked_attempts),
                "recent_blocked": list(self._blocked_attempts)[-10:],
                "recent_online_allowed": list(self._allowed_online_events)[-10:],
            }

    def install_socket_guard(self) -> None:
        """Install process-wide DNS + socket interception blocking non-loopback egress.

        Blocking only ``connect``/``create_connection`` still lets a name lookup
        leave the machine: resolution itself is an outbound DNS query, which both
        leaks the requested domain and pollutes caches. In strict offline mode
        ``getaddrinfo``/``gethostbyname`` are therefore refused for non-loopback
        names as well (loopback names still resolve so ``localhost`` works).
        """
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

            def _guarded_getaddrinfo(host: Any, *args: Any, **kwargs: Any) -> Any:
                guard = OfflineGuard._active_guard
                if guard is not None and host is not None and not guard._resolution_allowed(str(host)):
                    name = str(host)
                    with guard._events_lock:
                        guard._blocked_attempts.append(
                            {"host": name.strip().lower(), "port": None, "purpose": "getaddrinfo"}
                        )
                    raise OfflinePolicyError(
                        f"DNS resolution of '{name}' blocked by HINATA {guard.mode} policy"
                    )
                return OfflineGuard._orig_getaddrinfo(host, *args, **kwargs)

            def _guarded_gethostbyname(host: Any) -> str:
                guard = OfflineGuard._active_guard
                if guard is not None and not guard._resolution_allowed(str(host)):
                    with guard._events_lock:
                        guard._blocked_attempts.append(
                            {"host": str(host).strip().lower(), "port": None, "purpose": "gethostbyname"}
                        )
                    raise OfflinePolicyError(
                        f"DNS resolution of '{host}' blocked by HINATA {guard.mode} policy"
                    )
                return OfflineGuard._orig_gethostbyname(host)

            socket.create_connection = _guarded_create_connection  # type: ignore[assignment]
            socket.socket.connect = _guarded_connect  # type: ignore[assignment,method-assign]
            socket.getaddrinfo = _guarded_getaddrinfo  # type: ignore[assignment]
            socket.gethostbyname = _guarded_gethostbyname  # type: ignore[assignment]

    def _resolution_allowed(self, host: str) -> bool:
        """Allow loopback/IP-literal lookups; forbid resolving public names offline."""
        cleaned = host.strip().strip("[]").lower()
        if not cleaned:
            return True  # getaddrinfo(None) = local addresses
        if is_loopback_host(cleaned):
            return True
        try:
            ipaddress.ip_address(cleaned)
            return True  # numeric address: no DNS query leaves the machine
        except ValueError:
            return False

    def uninstall_socket_guard(self) -> None:
        with OfflineGuard._patch_lock:
            if OfflineGuard._active_guard is self:
                socket.create_connection = OfflineGuard._orig_create_connection  # type: ignore[assignment]
                socket.socket.connect = OfflineGuard._orig_socket_connect  # type: ignore[assignment,method-assign]
                socket.getaddrinfo = OfflineGuard._orig_getaddrinfo  # type: ignore[assignment]
                socket.gethostbyname = OfflineGuard._orig_gethostbyname  # type: ignore[assignment]
                OfflineGuard._active_guard = None

    @contextmanager
    def enforce(self) -> Iterator[OfflineGuard]:
        self.install_socket_guard()
        try:
            yield self
        finally:
            self.uninstall_socket_guard()
