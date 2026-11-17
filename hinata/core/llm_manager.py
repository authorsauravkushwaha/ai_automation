"""Loopback-only Ollama client with automatic local-service connection.

The client never contacts a remote AI host.  Before a model request it makes sure
the local Ollama runtime is answering and that a model is actually available, so a
normal install works even when the installer's service was not started yet.
"""

from __future__ import annotations

import json
import logging
import math
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator
from typing import Any

from hinata.config import OllamaConfig
from hinata.core.errors import CancellationToken, CircuitBreaker
from hinata.core.ollama_service import (
    DISABLED,
    MODEL_MISSING,
    NOT_INSTALLED,
    UNREACHABLE,
    OllamaService,
    find_ollama_executable,
)
from hinata.core.providers import (
    ContextManager,
    ModelCapabilities,
    ModelRouter,
    StructuredObservation,
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
        if config.hardware_profile == "lenovo_loq_i5":
            from hinata.core.jarvis_models import select_optimal_fleet_pack
            from hinata.core.providers import HardwareCapabilityService

            pack = select_optimal_fleet_pack(HardwareCapabilityService().detect())
            # Respect explicit owner pins; only fill unconfigured role slots.
            if config.model in {"", "qwen2.5:0.5b-instruct-q4_K_M"}:
                config.model = pack.chat_model
            config.planner_model = config.planner_model or pack.planner_model
            config.coding_model = config.coding_model or pack.coding_model
            config.reviewer_model = config.reviewer_model or pack.reviewer_model
            config.vision_model = config.vision_model or pack.vision_model
        self.config = config
        # Ignore environment proxies and refuse every redirect, including loopback redirects.
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
        self.base_url = config.host.rstrip("/")
        self._response_lock = threading.Lock()
        self._active_response: Any = None
        self._active_responses: dict[str, Any] = {}
        self._concurrency_semaphore = threading.Semaphore(1)
        self._service = service or OllamaService(config)
        # Model chosen at runtime when config.toml leaves ollama.model empty.
        self._resolved_model: str | None = None
        self._last_model_identity: str = ""
        self._fallback_events: list[dict[str, Any]] = []
        self.circuit_breaker = CircuitBreaker(failure_threshold=4, recovery_seconds=20.0)
        self.router = ModelRouter(
            config.model,
            config.embedding_model,
            role_overrides={
                "planner": config.planner_model,
                "coder": config.coding_model,
                "reviewer": config.reviewer_model,
                "vision": config.vision_model,
            },
            allow_fallback=bool(config.allow_model_fallback and not config.strict_model_pin),
        )
        self.context_manager = ContextManager(config.context_tokens, config.max_tokens)

    # ------------------------------------------------------------- connection
    @property
    def service(self) -> OllamaService:
        return self._service

    @property
    def last_model_identity(self) -> str:
        return self._last_model_identity

    @property
    def fallback_events(self) -> list[dict[str, Any]]:
        return list(self._fallback_events)

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

    def cancel_active_request(self, generation_id: str | None = None) -> None:
        """Close an in-flight local HTTP response to stop waiting for model output."""
        with self._response_lock:
            if generation_id is not None:
                targets = [self._active_responses.pop(generation_id, None)]
            else:
                targets = [self._active_response, *self._active_responses.values()]
                self._active_responses.clear()
        for response in targets:
            if response is not None:
                try:
                    response.close()
                except OSError:
                    pass

    def list_models(self, *, timeout: int = 3) -> list[dict[str, Any]]:
        """Public provider-neutral model listing with digest/metadata."""
        payload = self._request("/api/tags", timeout=timeout)
        raw = payload.get("models")
        if not isinstance(raw, list):
            return []
        return [item for item in raw if isinstance(item, dict)]

    def embed_texts(self, model: str, texts: list[str]) -> list[list[float]]:
        """Public provider-neutral embedding call with vector normalization."""
        if not 1 <= len(texts) <= 8:
            raise ValueError("Embedding batch must contain 1..8 texts")
        result = self._request(
            "/api/embed",
            {"model": model, "input": texts, "truncate": False, "keep_alive": 0},
            timeout=self.config.timeout_seconds,
        )
        vectors = result.get("embeddings")
        if not isinstance(vectors, list) or len(vectors) != len(texts):
            raise OllamaError("Embedding model returned an invalid number of vectors")
        validated: list[list[float]] = []
        for vector in vectors:
            if not isinstance(vector, list) or not 1 <= len(vector) <= 8192:
                raise OllamaError("Embedding model returned an invalid vector")
            if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in vector):
                raise OllamaError("Embedding model returned a non-finite vector")
            norm = math.sqrt(sum(v * v for v in vector))
            if not math.isfinite(norm) or norm == 0:
                raise OllamaError("Embedding model returned a zero or invalid vector")
            validated.append([float(v / norm) for v in vector])
        if len({len(v) for v in validated}) != 1:
            raise OllamaError("Embedding dimensions do not match")
        return validated

    def capabilities(self, model: str | None = None) -> ModelCapabilities:
        target = model or self.config.model or "default"
        lower = target.lower()
        is_embed = "embed" in lower
        is_vision = any(tag in lower for tag in ("vl", "vision", "llava", "moondream"))
        quant = ""
        if "q4" in lower:
            quant = "Q4"
        elif "q8" in lower:
            quant = "Q8"
        return ModelCapabilities(
            model_name=target,
            provider="ollama_local",
            quantization=quant,
            context_length=self.config.context_tokens,
            supports_json_mode=not is_embed,
            supports_native_tools=any(k in lower for k in ("qwen2.5", "llama3.1", "llama3.2", "mistral")),
            supports_streaming=not is_embed,
            supports_embeddings=is_embed,
            supports_vision=is_vision,
            supports_cancellation=True,
            max_concurrency=1,
        )

    def unload_model(self, model: str | None = None) -> bool:
        target = model or self.config.model
        if not target:
            return False
        try:
            self._request("/api/generate", {"model": target, "keep_alive": 0}, timeout=5)
            return True
        except OllamaError:
            return False

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

    def _prepare_model(
        self,
        *,
        connect: bool,
        pull_missing: bool,
        role: str = "chat",
        explicit_fallback_consent: bool = False,
    ) -> tuple[str, dict[str, Any]]:
        """Ensure a usable model name, connecting or downloading when requested.

        Never silently substitutes another local model when a specific model is
        configured unless ``allow_model_fallback`` or ``explicit_fallback_consent``
        is enabled, and records every fallback event with digest identity.
        """
        if not self.circuit_breaker.allow("ollama_chat"):
            raise OllamaError("Local model circuit breaker is open after repeated failures; wait or press Reconnect.")
        self._service.use_base_url(self.base_url)
        report = self.ensure_connected() if connect else self._service.last_report()
        if report.get("state") == DISABLED:
            raise OllamaError("The local model is disabled in config.toml")

        raw_models: list[dict[str, Any]] | None
        models: list[str] | None
        try:
            tags_payload = self._request("/api/tags", timeout=3)
            raw_list = tags_payload.get("models")
            raw_models = [m for m in raw_list if isinstance(m, dict)] if isinstance(raw_list, list) else []
            models = self._model_names(tags_payload)
        except OllamaError as exc:
            # The chat endpoint is the final authority: a partially implemented or
            # still-starting server may reject /api/tags while serving /api/chat.
            LOGGER.debug("Ollama /api/tags probe failed: %s", exc)
            raw_models = None
            models = None

        if models is not None and not models:
            raise ModelUnavailableError(
                "No local model is installed. Run 'hinata model pull' "
                "or set ollama.model in config.toml."
            )
        if models is None:
            # Could not enumerate models; let the chat endpoint decide.
            wanted_model = self.router.model_for_role(role) or self.active_model([])
            if not wanted_model:
                raise ModelUnavailableError(
                    "No local model is installed. Run 'hinata model pull'."
                )
            self._last_model_identity = wanted_model
            return wanted_model, report

        selection = self.router.resolve(
            role,
            raw_models if raw_models is not None else models,
            explicit_fallback_consent=explicit_fallback_consent,
        )
        if selection.resolved_model:
            if selection.fallback_used:
                event = {
                    "role": role,
                    "requested": selection.requested_model,
                    "selected": selection.resolved_model,
                    "digest": selection.digest,
                    "note": selection.consent_note,
                    "timestamp": time.time(),
                }
                self._fallback_events.append(event)
                LOGGER.warning("%s", selection.consent_note)
            self._last_model_identity = (
                f"{selection.resolved_model}@{selection.digest}"
                if selection.digest
                else selection.resolved_model
            )
            return selection.resolved_model, report

        wanted = selection.requested_model
        if pull_missing and wanted:
            installed = self._service.install_model()
            if installed.get("state") != "ready" or not installed.get("model_installed"):
                raise ModelUnavailableError(
                    f"Could not download {wanted}: "
                    f"{installed.get('error') or installed.get('guidance') or 'unknown error'}"
                )
            self._last_model_identity = wanted
            return wanted, installed

        available_hint = ", ".join(models[:5])
        raise ModelUnavailableError(
            f"Configured model '{wanted}' is not installed locally (available: {available_hint}). "
            f"Run 'ollama pull {wanted}' during connected setup, or explicitly opt into "
            f"ollama.allow_model_fallback = true in config.toml."
        )

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
        role: str = "chat",
        cancel_token: CancellationToken | None = None,
    ) -> str:
        with self._concurrency_semaphore:
            if cancel_token is not None:
                cancel_token.check()
            model, report = self._prepare_model(connect=connect, pull_missing=pull_missing, role=role)
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
                if not connect:
                    self.circuit_breaker.record_failure("ollama_chat", str(exc))
                    raise self._explain(exc, report) from exc
                report = self.ensure_connected(force=True)
                try:
                    result = self._request("/api/chat", payload, timeout=self.config.timeout_seconds)
                except OllamaError as retry_exc:
                    self.circuit_breaker.record_failure("ollama_chat", str(retry_exc))
                    raise self._explain(retry_exc, report) from retry_exc
            self.circuit_breaker.record_success("ollama_chat")
            message = result.get("message")
            if not isinstance(message, dict) or not isinstance(message.get("content"), str):
                raise OllamaError("Ollama response did not contain assistant text")
            content = message["content"].strip()
            if not content:
                raise OllamaError("Ollama returned an empty response")
            return content

    def stream_chat(
        self,
        messages: list[dict[str, str]],
        system: str,
        *,
        on_token: Callable[[str], None] | None = None,
        connect: bool = True,
        format: str | None = None,
        num_predict: int | None = None,
        role: str = "chat",
        cancel_token: CancellationToken | None = None,
        generation_id: str | None = None,
    ) -> Iterator[str]:
        """End-to-end streaming completion with generation ID and token-level cancellation."""
        gen_id = generation_id or f"gen-{int(time.monotonic() * 1000)}"
        with self._concurrency_semaphore:
            if cancel_token is not None:
                cancel_token.check()
                cancel_token.on_cancel(lambda: self.cancel_active_request(gen_id))
            model, report = self._prepare_model(connect=connect, pull_missing=False, role=role)
            payload: dict[str, Any] = {
                "model": model,
                "stream": True,
                "keep_alive": self.config.keep_alive,
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
            data = json.dumps(payload).encode("utf-8")
            request = urllib.request.Request(
                self.base_url + "/api/chat",
                data=data,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            try:
                response = self._opener.open(request, timeout=self.config.timeout_seconds)
            except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
                self.circuit_breaker.record_failure("ollama_chat", str(exc))
                raise self._explain(
                    OllamaError(f"Cannot reach local Ollama at {self.base_url}: {exc}"),
                    report,
                ) from exc
            with self._response_lock:
                self._active_responses[gen_id] = response
                self._active_response = response
            try:
                with response:
                    for raw_line in response:
                        if cancel_token is not None and cancel_token.is_cancelled:
                            break
                        if not raw_line or not raw_line.strip():
                            continue
                        chunk_obj = json.loads(raw_line.decode("utf-8"))
                        msg = chunk_obj.get("message")
                        if isinstance(msg, dict) and isinstance(msg.get("content"), str):
                            token = msg["content"]
                            if token:
                                if on_token is not None:
                                    on_token(token)
                                yield token
                        if chunk_obj.get("done"):
                            break
                self.circuit_breaker.record_success("ollama_chat")
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                raise OllamaError("Ollama returned an invalid streaming chunk") from exc
            except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
                if cancel_token is not None and cancel_token.is_cancelled:
                    return
                self.circuit_breaker.record_failure("ollama_chat", str(exc))
                raise OllamaError(f"Streaming connection interrupted: {exc}") from exc
            finally:
                with self._response_lock:
                    self._active_responses.pop(gen_id, None)
                    if self._active_response is response:
                        self._active_response = None

    @staticmethod
    def _schemas_for_query(user_text: str, schemas: dict[str, Any]) -> dict[str, Any]:
        """Keep only tools relevant to this request, shrinking the prompt dramatically."""
        text = user_text.lower()
        groups = {
            "web": {"web_search", "fetch_webpage"},
            "apps": {"open_application", "close_application", "list_installed_apps"},
            "coding": {"write_code", "code_inspect", "inspect_repository_imports", "build_project", "workspace_list", "workspace_read", "workspace_search"},
            "files": {"open_file", "open_folder", "list_directory", "search_files", "search_file_content", "read_text_file", "write_text_file", "create_folder"},
            "knowledge": {name for name in schemas if name.startswith("knowledge_")},
            "memory": {"memory_search", "remember_preference", "list_preferences"},
            "system": {"system_info", "hardware_profile", "ai_fleet_profile", "list_processes", "volume"},
        }
        selected: set[str] = set()
        if any(w in text for w in ("search", "internet", "web", "google", "website", "url", "fetch")):
            selected |= groups["web"] | groups["knowledge"]
        if any(w in text for w in ("app", "open", "launch", "close", "installed", "start")):
            selected |= groups["apps"]
        if any(w in text for w in ("code", "program", "script", "function", "class", "python", "javascript", "java", "rust", "c++", "html")):
            selected |= groups["coding"]
        if any(w in text for w in ("file", "folder", "directory", "document", "read", "write")):
            selected |= groups["files"]
        if any(w in text for w in ("remember", "memory", "preference", "name")):
            selected |= groups["memory"]
        if any(w in text for w in ("system", "computer", "hardware", "volume", "process")):
            selected |= groups["system"]
        # Conversational requests need no tool catalog; action-style queries receive at most 14 schemas.
        action_terms = ("open ", "launch ", "search ", "google ", "fetch ", "write ", "program ", "script ", "create ", "read ", "list ", "close ", "find ", "run ", "inspect ", "analyze ")
        if not any(term in text for term in action_terms):
            return {}
        matched_names = [name for name in schemas if name in selected]
        return {name: schemas[name] for name in matched_names[:14]}

    def _completion_budget(self, text: str, role: str) -> int:
        if role == "coder" or any(word in text.lower() for word in ("write code", "program in", "implement", "generate code")):
            return max(1536, min(3072, self.config.max_tokens * 6))
        if any(word in text.lower() for word in ("search", "research", "explain", "compare", "everything", "from start")):
            return max(900, min(1536, self.config.max_tokens * 3))
        return max(384, min(768, self.config.max_tokens * 2))

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
        role: str = "chat",
        retrieved_memories: list[dict[str, Any]] | None = None,
        observations: list[StructuredObservation] | None = None,
        cancel_token: CancellationToken | None = None,
    ) -> str:
        preference_text = "; ".join(f"{row['key']}={row['value']}" for row in preferences[:20])
        relevant_schemas = self._schemas_for_query(user_text, schemas)
        base_system = f"""You are HINATA, a local-first personal assistant running on the owner's Windows laptop.
Use this response style: {personality_instruction}
Be honest about uncertainty; never claim an action succeeded before tool verification.
Return ONE compact JSON object. For chat replies, answer naturally and completely; do not apply a word limit.
Select one of these forms:
{{"type":"tool","tool":"TOOL_NAME","arguments":{{}}}}
{{"type":"plan","objective":"desired result","steps":[{{"step_id":"step_1","tool":"TOOL_NAME","arguments":{{}},"depends_on":[]}}]}}
{{"type":"chat","content":"helpful response"}}
{{"type":"clarify","content":"one specific question"}}
Relevant tools and exact argument rules: {json.dumps(relevant_schemas, separators=(',', ':'))}
Plans are bounded, dependency-validated graphs and require owner approval or an approved low-risk autonomy policy before steps run.
Rules:
- Use a tool only when the user requests an action it can actually perform.
- Never invent tools, arguments, paths, command lines, or action results.
- Use clarify if a path, application, or other required detail is ambiguous.
- For general questions or small talk (such as "Hello") use chat; do not claim to have performed an action.
- Example: user says "Hello" -> {{"type":"chat","content":"Hello! How can I help you today?"}}
- When in doubt, answer with type chat rather than guessing a tool.
- Approved user preferences: {preference_text or 'none'}
- Voice and speaker scores are not authentication. Sensitive tools require separate approval.
"""
        system, messages, _audit = self.context_manager.assemble(
            system_prompt=base_system,
            user_text=user_text,
            history=history,
            retrieved_memories=retrieved_memories,
            observations=observations,
        )
        # Reasoning models (e.g. deepseek-r1, qwq) often fail HTTP 400 or break <think>
        # chains when forced into strict Ollama `format="json"` grammar; parse_llm_plan
        # extracts the JSON payload after stripping <think>...</think> blocks.
        active_role_model = (self.router.model_for_role(role) or "").lower()
        use_json_format = not any(tag in active_role_model for tag in ("deepseek-r1", "qwq", "r1:"))
        try:
            raw_reply = self.chat(
                messages,
                system,
                connect=connect,
                pull_missing=pull_missing,
                format="json" if use_json_format else None,
                role=role,
                keep_alive=self.config.keep_alive,
                num_predict=self._completion_budget(user_text, role),
                cancel_token=cancel_token,
            )
        except OllamaError as exc:
            # Older builds or reasoning models may reject the "format" field; retry once without it.
            if "format" not in str(exc).lower() or isinstance(exc, ModelUnavailableError):
                raise
            raw_reply = self.chat(
                messages,
                system,
                connect=connect,
                pull_missing=pull_missing,
                role=role,
                keep_alive=self.config.keep_alive,
                num_predict=self._completion_budget(user_text, role),
                cancel_token=cancel_token,
            )
        if "<think>" in raw_reply.lower():
            from hinata.core.jarvis_models import strip_reasoning_traces

            return strip_reasoning_traces(raw_reply)
        return raw_reply

    def benchmark(self, prompt: str = "Reply with JSON: {\"ok\": true}") -> dict[str, Any]:
        """Run a lightweight local latency and token-throughput benchmark against loopback Ollama."""
        started = time.monotonic()
        first_token_ms: float | None = None
        chunks: list[str] = []

        def _on_tok(piece: str) -> None:
            nonlocal first_token_ms
            if first_token_ms is None:
                first_token_ms = round((time.monotonic() - started) * 1000.0, 2)
            chunks.append(piece)

        try:
            list(
                self.stream_chat(
                    [{"role": "user", "content": prompt}],
                    "Return a short JSON reply.",
                    on_token=_on_tok,
                    num_predict=32,
                )
            )
            elapsed_ms = round((time.monotonic() - started) * 1000.0, 2)
            full_text = "".join(chunks)
            est_tokens = max(1, len(full_text) // 4)
            tps = round(est_tokens / max(0.001, elapsed_ms / 1000.0), 2)
            return {
                "available": True,
                "model_identity": self._last_model_identity,
                "time_to_first_token_ms": first_token_ms or elapsed_ms,
                "total_latency_ms": elapsed_ms,
                "estimated_output_tokens": est_tokens,
                "estimated_tokens_per_second": tps,
            }
        except OllamaError as exc:
            return {
                "available": False,
                "error": str(exc),
                "model_identity": self.config.model,
            }
