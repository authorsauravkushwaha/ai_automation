"""Loopback-only Ollama client with small-context CPU defaults."""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from typing import Any

from hinata.config import OllamaConfig


class OllamaError(RuntimeError):
    pass


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Do not let a loopback service redirect private prompts elsewhere."""

    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> None:
        return None


class LLMManager:
    def __init__(self, config: OllamaConfig) -> None:
        config.validate()
        self.config = config
        # Ignore environment proxies and refuse every redirect, including loopback redirects.
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
        self.base_url = config.host.rstrip("/")
        self._response_lock = threading.Lock()
        self._active_response: Any = None

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

    def status(self) -> dict[str, Any]:
        try:
            result = self._request("/api/tags", timeout=2)
            models = [str(item.get("name", "")) for item in result.get("models", []) if isinstance(item, dict)]
            installed = self.config.model in models or any(
                name.split(":")[0] == self.config.model.split(":")[0] and self.config.model.count(":") == 0
                for name in models
            )
            return {
                "available": True,
                "model": self.config.model,
                "installed": installed,
                "models": models,
                "host": self.base_url,
            }
        except OllamaError as exc:
            return {
                "available": False,
                "model": self.config.model,
                "installed": False,
                "models": [],
                "host": self.base_url,
                "error": str(exc),
            }

    def chat(self, messages: list[dict[str, str]], system: str) -> str:
        payload = {
            "model": self.config.model,
            "stream": False,
            "keep_alive": 0,  # Release model RAM when the response is complete.
            # Decode-time guarantee of syntactically valid JSON; the schema is
            # still enforced by the caller (IntentParser/Planner).
            "format": "json",
            "messages": [{"role": "system", "content": system}, *messages],
            "options": {
                "temperature": self.config.temperature,
                "num_predict": self.config.max_tokens,
                "num_ctx": self.config.context_tokens,
                "num_thread": self.config.threads,
            },
        }
        result = self._request("/api/chat", payload, timeout=self.config.timeout_seconds)
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
    ) -> str:
        preference_text = "; ".join(f"{row['key']}={row['value']}" for row in preferences[:20])
        system = f"""You are HINATA, a local-first personal assistant running on the owner's Windows laptop.
All processing is local. Use this response style: {personality_instruction}
Be honest about uncertainty; do not claim human feelings or claim an action succeeded before tool verification.
Return ONE JSON object and nothing else. Never output Markdown.
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
- For general questions or small talk (such as "Hello") use chat; do not claim to have performed an action.
- Example: user says "Hello" -> {{"type":"chat","content":"Hello! How can I help you today?"}}
- When in doubt, answer with type chat rather than guessing a tool.
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
        return self.chat(messages, system)
