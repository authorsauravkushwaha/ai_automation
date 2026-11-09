"""Locate, start, and verify the loopback-only Ollama runtime.

HINATA talks to Ollama over ``127.0.0.1`` only.  A fresh install, a reboot, or a
closed terminal used to leave that local service stopped, so chat requests failed
with "the local model did not return a valid HINATA JSON plan".  This module makes
the connection automatic: it reuses an already-running service, otherwise it finds
``ollama.exe`` (including the default per-user install location that is not always
on ``PATH`` yet) and starts ``ollama serve`` in the background before waiting for
the loopback API to answer.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from hinata.config import OllamaConfig

LOGGER = logging.getLogger(__name__)

READY = "ready"
MODEL_MISSING = "model_missing"
NOT_INSTALLED = "not_installed"
UNREACHABLE = "unreachable"
DISABLED = "disabled"

INSTALL_GUIDANCE = (
    "Ollama is not installed. Install it from https://ollama.com/download/windows, "
    "or run: winget install -e --id Ollama.Ollama"
)
MODEL_GUIDANCE = "Run 'hinata model pull' to download the configured local model."

# Ollama's Windows installer registers these locations but only updates the user
# PATH for new terminals, so an existing shell may not resolve "ollama" yet.
_WINDOWS_INSTALL_CANDIDATES: tuple[tuple[str, str], ...] = (
    ("LOCALAPPDATA", "Programs/Ollama/ollama.exe"),
    ("ProgramFiles", "Ollama/ollama.exe"),
    ("ProgramW6432", "Ollama/ollama.exe"),
    ("ProgramFiles(x86)", "Ollama/ollama.exe"),
)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Do not let a loopback service redirect local prompts elsewhere."""

    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> None:
        return None


def find_ollama_executable() -> str | None:
    """Return an ``ollama`` executable path, or ``None`` when it is not installed."""
    for name in ("ollama.exe", "ollama"):
        found = shutil.which(name)
        if found:
            return found
    for variable, relative in _WINDOWS_INSTALL_CANDIDATES:
        base = os.environ.get(variable)
        if not base:
            continue
        candidate = Path(base) / relative
        try:
            if candidate.is_file():
                return str(candidate)
        except OSError:
            continue
    return None


def _loopback_host(value: str) -> str:
    """Reduce a validated loopback URL to the ``host:port`` form Ollama expects."""
    parts = urlsplit(value)
    host = parts.hostname or "127.0.0.1"
    port = parts.port or 11434
    return f"{host}:{port}"


class OllamaService:
    """Idempotent local-runtime supervisor; never contacts a non-loopback host."""

    def __init__(self, config: OllamaConfig) -> None:
        config.validate()
        self.config = config
        self.base_url = config.host.rstrip("/")
        # Ignore environment proxies and refuse redirects, matching the chat client.
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
        self._lock = threading.Lock()
        self._state: dict[str, Any] | None = None
        self._last_attempt: float | None = None
        self._executable: str | None = None
        self._process: subprocess.Popen[bytes] | None = None
        # Do not respawn a hopeless server on every single message.
        self.retry_after_seconds = 30.0

    # ------------------------------------------------------------------ probes
    def _probe(self, timeout: float = 1.5) -> dict[str, Any] | None:
        request = urllib.request.Request(self.base_url + "/api/tags", method="GET")
        try:
            with self._opener.open(request, timeout=timeout) as response:
                payload = json.loads(response.read(1_000_000).decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, OSError, ValueError):
            return None
        return payload if isinstance(payload, dict) else None

    def _port_open(self, timeout: float = 0.4) -> bool:
        """True when something already listens on the configured loopback port."""
        parts = urlsplit(self.base_url)
        try:
            with socket.create_connection((parts.hostname or "127.0.0.1", parts.port or 11434), timeout):
                return True
        except OSError:
            return False

    @staticmethod
    def _model_names(payload: dict[str, Any]) -> list[str]:
        models = payload.get("models")
        if not isinstance(models, list):
            return []
        return [str(item.get("name", "")) for item in models if isinstance(item, dict)]

    def _model_present(self, models: list[str]) -> bool:
        wanted = self.config.model
        for name in models:
            if name == wanted:
                return True
            # "qwen2.5:0.5b-instruct-q4_K_M" may be listed with a ":latest" tag.
            if wanted.count(":") == 0 and name.split(":")[0] == wanted:
                return True
            if name.split(":")[0] == wanted.split(":")[0] and wanted.count(":") == 1:
                if name.endswith(wanted.split(":", 1)[1]):
                    return True
        return False

    def use_base_url(self, value: str) -> None:
        """Follow a loopback endpoint override made on the chat client."""
        parts = urlsplit(value)
        if parts.scheme == "http" and parts.hostname in {"127.0.0.1", "localhost", "::1"}:
            self.base_url = value.rstrip("/")

    # ------------------------------------------------------------- public state
    def quick_status(self) -> dict[str, Any]:
        """Probe the service without starting anything (safe for UI refresh)."""
        payload = self._probe(timeout=1.0)
        if payload is None:
            executable = self._executable or find_ollama_executable()
            self._executable = executable
            state = NOT_INSTALLED if executable is None else UNREACHABLE
            report = self._report(state, executable=executable, service_ready=False)
            with self._lock:
                if self._state is None or self._state.get("state") != READY:
                    self._state = report
            return report
        with self._lock:
            self._state = self._ready_report(payload, started=False)
            return dict(self._state)

    def last_report(self) -> dict[str, Any]:
        """Return the cached report without touching the network."""
        with self._lock:
            return dict(self._state or {})

    def ensure_running(
        self,
        *,
        wait_seconds: float = 25.0,
        force: bool = False,
        start_if_stopped: bool | None = None,
    ) -> dict[str, Any]:
        """Return a ready loopback service, starting ``ollama serve`` when needed.

        Concurrent callers are serialized so a burst of messages cannot spawn
        several servers.  A failed start is remembered for
        ``retry_after_seconds`` unless ``force`` is set.
        """
        if not self.config.enabled:
            report = self._report(DISABLED, executable=None, service_ready=False)
            with self._lock:
                self._state = report
            return dict(report)

        may_start = self.config.auto_start if start_if_stopped is None else start_if_stopped

        with self._lock:
            payload = self._probe()
            if payload is not None:
                self._state = self._ready_report(payload, started=False)
                return dict(self._state)

            executable = self._executable or find_ollama_executable()
            self._executable = executable
            if executable is None:
                # Something may still be listening under another manager (a service,
                # a container, or a test stub).  Never hard-fail for that case.
                listening = self._port_open()
                self._state = self._report(
                    UNREACHABLE if listening else NOT_INSTALLED,
                    executable=None,
                    service_ready=False,
                    error="The loopback port is open but Ollama did not answer" if listening else None,
                )
                return dict(self._state)
            if not may_start:
                self._state = self._report(
                    UNREACHABLE,
                    executable=executable,
                    service_ready=False,
                    error="Automatic start is disabled (ollama.auto_start = false)",
                )
                return dict(self._state)

            now = time.monotonic()
            if (
                not force
                and self._state is not None
                and self._state.get("state") in {UNREACHABLE, MODEL_MISSING}
                and self._last_attempt is not None
                and now - self._last_attempt < self.retry_after_seconds
            ):
                return dict(self._state)

            self._last_attempt = now
            started = self._spawn_server(executable)
            if not started:
                self._state = self._report(
                    UNREACHABLE,
                    executable=executable,
                    service_ready=False,
                    error=f"Could not start {executable}",
                )
                return dict(self._state)

            deadline = time.monotonic() + max(1.0, wait_seconds)
            while time.monotonic() < deadline:
                time.sleep(0.25)
                payload = self._probe(timeout=1.0)
                if payload is not None:
                    self._state = self._ready_report(payload, started=True)
                    return dict(self._state)

            self._state = self._report(
                UNREACHABLE,
                executable=executable,
                service_ready=False,
                error=f"Ollama did not answer at {self.base_url}",
            )
            LOGGER.warning("Ollama did not answer at %s after starting it", self.base_url)
            return dict(self._state)

    # ------------------------------------------------------------------ helpers
    def _ready_report(self, payload: dict[str, Any], *, started: bool) -> dict[str, Any]:
        models = self._model_names(payload)
        installed = self._model_present(models)
        report = self._report(
            READY if installed else MODEL_MISSING,
            executable=self._executable,
            service_ready=True,
            error=None if installed else f"Model {self.config.model} is not downloaded",
        )
        report["model_installed"] = installed
        report["models"] = models
        report["started_by_hinata"] = started
        return report

    def _report(
        self,
        state: str,
        *,
        executable: str | None,
        service_ready: bool,
        error: str | None = None,
    ) -> dict[str, Any]:
        guidance = None
        if state == NOT_INSTALLED:
            guidance = INSTALL_GUIDANCE
        elif state == MODEL_MISSING:
            guidance = MODEL_GUIDANCE
        elif state == UNREACHABLE:
            guidance = "Start Ollama and press Reconnect, or run 'ollama serve' once."
        elif state == DISABLED:
            guidance = "Set ollama.enabled = true in config.toml to use the local model."
        return {
            "state": state,
            "service_ready": service_ready,
            "auto_start": self.config.auto_start,
            "model": self.config.model,
            "model_installed": state == READY,
            "models": [],
            "host": self.base_url,
            "executable": executable,
            "error": error,
            "guidance": guidance,
        }

    def _spawn_server(self, executable: str) -> bool:
        """Start ``ollama serve`` detached from this process, without a console."""
        environment = os.environ.copy()
        environment["OLLAMA_HOST"] = _loopback_host(self.config.host)
        creationflags = 0
        startupinfo = None
        if os.name == "nt":  # pragma: no cover - exercised on Windows only
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            creationflags |= getattr(subprocess, "DETACHED_PROCESS", 0)
            startinfo_class = getattr(subprocess, "STARTUPINFO", None)
            if startinfo_class is not None:
                startupinfo = startinfo_class()
                startupinfo.dwFlags |= getattr(subprocess, "STARTF_USESHOWWINDOW", 1)
                startupinfo.wShowWindow = getattr(subprocess, "SW_HIDE", 0)
        try:
            self._process = subprocess.Popen(
                [executable, "serve"],
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
                creationflags=creationflags,
                startupinfo=startupinfo,
            )
        except OSError as exc:
            LOGGER.warning("Could not start the local Ollama service: %s", exc)
            return False
        LOGGER.info("Started the local Ollama service from %s", executable)
        return True

    def install_model(self, *, timeout: int = 1800) -> dict[str, Any]:
        """Download the configured model with ``ollama pull`` (setup step only)."""
        executable = self._executable or find_ollama_executable()
        self._executable = executable
        if executable is None:
            return self._report(NOT_INSTALLED, executable=None, service_ready=False)
        self.ensure_running()
        environment = os.environ.copy()
        environment["OLLAMA_HOST"] = _loopback_host(self.config.host)
        try:
            result = subprocess.run(
                [executable, "pull", self.config.model],
                env=environment,
                shell=False,
                check=False,
                timeout=timeout,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return {
                **self._report(UNREACHABLE, executable=executable, service_ready=True),
                "error": str(exc),
            }
        payload = self._probe(timeout=2.0)
        if payload is not None:
            with self._lock:
                self._state = self._ready_report(payload, started=False)
                report = dict(self._state)
        else:
            report = self._report(UNREACHABLE, executable=executable, service_ready=False)
        report["returncode"] = int(result.returncode)
        return report
