"""Loopback-only Ollama client with automatic local-service connection.

The client never contacts a remote AI host.  Before a model request it makes sure
the local Ollama runtime is answering and that a model is actually available, so a
normal install works even when the installer's service was not started yet.
"""

from __future__ import annotations

import json
import logging
import threading
import urllib.error
import urllib.request
from typing import Any

from hinata.config import OllamaConfig
from hinata.core.ollama_service import (
    DISABLED,
    MODEL_MISSING,
    NOT_INSTALLED,
    UNREACHABLE,
    OllamaService,
    find_ollama_executable,
)

LOGGER = logging.getLogger(__name__)


class OllamaError(RuntimeError):
    pass


class ModelUnavailableError(OllamaError):
    """The runtime is reachable but the configured model is not downloaded."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Do not let a loopback service redirect private prompts elsewhere."""

    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> None:
        return None


class LLMManager:
    def __init__(self, config: OllamaConfig, service: OllamaService | None = None) -> None:
        config.validate()
        self.config = config
        # Ignore environment proxies and refuse every redirect, including loopback redirects.
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
        self.base_url = config.host.rstrip("/")
        self._response_lock = threading.Lock()
        self._active_response: Any = None
        self._service = service or OllamaService(config)
        # Model chosen at runtime when config.toml leaves ollama.model empty.
        self._resolved_model: str | None = None

    # ------------------------------------------------------------- connection
    @property
    def service(self) -> OllamaService:
        return self._service

    def ensure_connected(self, *, force: bool = False) -> dict[str, Any]:
        """Connect to (or start) loopback Ollama; safe to call before any request."""
        self._service.use_base_url(self.base_url)
        report = self._service.ensure_running(force=force)
        if report.get("state") == NOT_INSTALLED and force and find_ollama_executable():
            # Ollama may have been installed after HINATA started.
            report = self._service.ensure_running(force=True)
        return report

    def connect_status(self) -> dict[str, Any]:
        """Cheap, non-starting status probe for status bars and diagnostics."""
        self._service.use_base_url(self.base_url)
        return self._service.quick_status()

    def cancel_active_request(self) -> None:
        """Close an in-flight local HTTP response to stop waiting for model output."""
        with self._response_lock:
            response = self._active_response
        if response is not None:
            try:
                response.close()
            except OSError:
                pass

    def _request(self, endpoint: str, payload: dict[str, Any] | None = None, timeout: int = 5) -> dict[str, Any]:
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            self.base_url + endpoint,
            data=data,
            headers={"Content-Type": "application/json"},
            method="GET" if data is None else "POST",
        )
        try:
            response = self._opener.open(request, timeout=timeout)
            with self._response_lock:
                self._active_response = response
            try:
                with response:
                    raw = response.read(2_000_000)
                result = json.loads(raw.decode("utf-8"))
            finally:
                with self._response_lock:
                    if self._active_response is response:
                        self._active_response = None
        except urllib.error.HTTPError as exc:
            detail = exc.read(1000).decode("utf-8", errors="replace")
            raise OllamaError(f"Ollama returned HTTP {exc.code}: {detail}") from exc
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise OllamaError("Ollama returned an invalid response") from exc
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
            raise OllamaError(f"Cannot reach local Ollama at {self.base_url}: {exc}") from exc
        if not isinstance(result, dict):
            raise OllamaError("Ollama returned an unexpected response")
        return result

    @staticmethod
    def _model_names(result: dict[str, Any]) -> list[str]:
        models = result.get("models")
        if not isinstance(models, list):
            return []
        return [str(item.get("name", "")) for item in models if isinstance(item, dict)]

    def _model_installed(self, model: str, models: list[str]) -> bool:
        if not model:
            return bool(models)
        if model in models:
            return True
        base, _, tag = model.partition(":")
        if not tag:
            return any(name.split(":")[0] == base for name in models)
        return any(name.split(":")[0] == base and name.endswith(tag) for name in models)

    def active_model(self, models: list[str] | None = None) -> str:
        """Return the configured model, or the first local model if none is set."""
        if self.config.model:
            return self.config.model
        if self._resolved_model:
            return self._resolved_model
        names = models
        if names is None:
            tags = self._request("/api/tags", timeout=2)
            names = self._model_names(tags)
        self._resolved_model = names[0] if names else ""
        return self._resolved_model

    def status(self) -> dict[str, Any]:
        try:
            result = self._request("/api/tags", timeout=2)
        except OllamaError as exc:
            return {
                "available": False,
                "model": self.config.model,
                "installed": False,
                "models": [],
                "host": self.base_url,
                "error": str(exc),
                "service": self._service.last_report(),
            }
        models = self._model_names(result)
        model = self.active_model(models)
        return {
            "available": True,
            "model": model,
            "installed": self._model_installed(model, models),
            "models": models,
            "host": self.base_url,
        }

    def _prepare_model(self, *, connect: bool, pull_missing: bool) -> tuple[str, dict[str, Any]]:
        """Ensure a usable model name, connecting or downloading when requested."""
        self._service.use_base_url(self.base_url)
        report = self.ensure_connected() if connect else self._service.last_report()
        if report.get("state") == DISABLED:
            raise OllamaError("The local model is disabled in config.toml")

        models: list[str] | None
        try:
            models = self._model_names(self._request("/api/tags", timeout=3))
        except OllamaError as exc:
            # The chat endpoint is the final authority: a partially implemented or
            # still-starting server may reject /api/tags while serving /api/chat.
            LOGGER.debug("Ollama /api/tags probe failed: %s", exc)
            models = None

        if models is not None and not models:
            raise ModelUnavailableError(
                "No local model is installed. Run 'hinata model pull' "
                "or set ollama.model in config.toml."
            )
        if models is None:
            # Could not enumerate models; let the chat endpoint decide.
            model = self.active_model([])
            if not model:
                raise ModelUnavailableError(
                    "No local model is installed. Run 'hinata model pull'."
                )
            return model, report

        model = self.active_model(models)
        if not model:
            raise ModelUnavailableError(
                "No local model is installed. Run 'hinata model pull' "
                "or set ollama.model in config.toml."
            )
        if not self._model_installed(model, models):
            if pull_missing and self.config.model:
                installed = self._service.install_model()
                if installed.get("state") != "ready" or not installed.get("model_installed"):
                    raise ModelUnavailableError(
                        f"Could not download {model}: "
                        f"{installed.get('error') or installed.get('guidance') or 'unknown error'}"
                    )
                report = installed
            elif self.config.model:
                # The configured model is absent but the PC already has a local one.
                # Using it keeps chat working instead of failing on a technicality.
                LOGGER.warning(
                    "Configured model %s is not downloaded; using the installed model %s",
                    self.config.model,
                    models[0],
                )
                model = models[0]
        return model, report

    def _explain(self, exc: OllamaError, report: dict[str, Any]) -> OllamaError:
        """Attach installer/startup guidance to a failed local request."""
        state = report.get("state")
        guidance = report.get("guidance")
        if state in {NOT_INSTALLED, UNREACHABLE, DISABLED, MODEL_MISSING} and guidance:
            return OllamaError(f"{exc} ({guidance})")
        return exc

    def chat(
        self,
        messages: list[dict[str, str]],
        system: str,
        *,
        connect: bool = True,
        pull_missing: bool = False,
        format: str | None = None,
        keep_alive: int | str = 0,
        num_predict: int | None = None,
    ) -> str:
        model, report = self._prepare_model(connect=connect, pull_missing=pull_missing)
        payload: dict[str, Any] = {
            "model": model,
            "stream": False,
            "keep_alive": keep_alive,  # Release model RAM when the response is complete.
            "messages": [{"role": "system", "content": system}, *messages],
            "options": {
                "temperature": self.config.temperature,
                "num_predict": self.config.max_tokens if num_predict is None else num_predict,
                "num_ctx": self.config.context_tokens,
                "num_thread": self.config.threads,
            },
        }
        if format:
            payload["format"] = format
        try:
            result = self._request("/api/chat", payload, timeout=self.config.timeout_seconds)
        except OllamaError as exc:
            # A service that was just restarted (or a model that was unloaded) can
            # drop the first request; reconnect once before giving up.
            if not connect:
                raise self._explain(exc, report) from exc
            report = self.ensure_connected(force=True)
            try:
                result = self._request("/api/chat", payload, timeout=self.config.timeout_seconds)
            except OllamaError as retry_exc:
                raise self._explain(retry_exc, report) from retry_exc
        message = result.get("message")
        if not isinstance(message, dict) or not isinstance(message.get("content"), str):
            raise OllamaError("Ollama response did not contain assistant text")
        content = message["content"].strip()
        if not content:
            raise OllamaError("Ollama returned an empty response")
        return content

    def complete(
        self,
        user_text: str,
        history: list[dict[str, Any]],
        preferences: list[dict[str, Any]],
        schemas: dict[str, Any],
        personality_instruction: str = "Be warm, clear, and helpful.",
        *,
        connect: bool = True,
        pull_missing: bool = False,
    ) -> str:
        preference_text = "; ".join(f"{row['key']}={row['value']}" for row in preferences[:20])
        system = f"""You are HINATA, a local-first personal assistant running on the owner's Windows laptop.
All processing is local. Use this response style: {personality_instruction}
Be honest about uncertainty; do not claim human feelings or claim an action succeeded before tool verification.
Return ONE JSON object and nothing else. Never output Markdown.
Keep "content" under 80 words so the object always finishes.
Select one of these forms:
{{"type":"tool","tool":"TOOL_NAME","arguments":{{}}}}
{{"type":"plan","objective":"desired result","steps":[{{"tool":"TOOL_NAME","arguments":{{}}}}]}}
{{"type":"chat","content":"helpful response"}}
{{"type":"clarify","content":"one specific question"}}
Available tools and exact argument rules: {json.dumps(schemas, separators=(',', ':'))}
Plans are limited to five validated low-impact steps and require owner approval before any step runs.
Plan steps may use only open_application, open_folder, search_files, create_folder, read_text_file, volume, system_info, memory_search, list_preferences, or set_personality_mode.
Rules:
- Use a tool only when the user requests an action it can actually perform.
- Never invent tools, arguments, paths, command lines, or action results.
- Use clarify if a path, application, or other required detail is ambiguous.
- For general questions use chat; do not claim to have performed an action.
- Approved user preferences: {preference_text or 'none'}
- Voice and speaker scores are not authentication. Sensitive tools require separate approval.
"""
        messages = [
            {"role": str(row["role"]), "content": str(row["content"])[:2000]}
            for row in history[-8:]
            if row.get("role") in {"user", "assistant"}
        ]
        if not messages or messages[-1].get("content") != user_text:
            messages.append({"role": "user", "content": user_text})
        # JSON mode keeps a small model from wrapping its answer in prose.  Some
        # builds ignore the flag, so parse_llm_plan still scans for the object.
        try:
            return self.chat(
                messages, system, connect=connect, pull_missing=pull_missing, format="json"
            )
        except OllamaError as exc:
            # Older builds reject the "format" field; retry once without it.
            if "format" not in str(exc).lower() or isinstance(exc, ModelUnavailableError):
                raise
            return self.chat(messages, system, connect=connect, pull_missing=pull_missing)
