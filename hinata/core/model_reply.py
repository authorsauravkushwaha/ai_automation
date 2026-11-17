"""Parse a local model's JSON reply from a small model's noisy output.

A 0.5B model does not always emit perfectly structured JSON, so this module keeps
a strict validator as the gate for every action while still recovering the intent
from small, common formatting mistakes (unquoted chat/clarify text, plan steps
without an ``arguments`` key, prose wrapped around the object).
"""

from __future__ import annotations

import ast
import json
import re
from typing import Any

CHAT_TYPES = {"chat", "clarify"}
CHAT_TEXT_KEYS = ("content", "response", "message", "answer", "text", "reply", "output")
ACTION_KEYS = frozenset({"tool", "arguments", "objective", "steps"})


class ModelReplyError(ValueError):
    """The model replied, but not with anything HINATA can safely act on."""


def _salvage_text_object(text: str) -> dict[str, Any] | None:
    """Recover ``{"type":"chat","content":free text}`` from slightly broken JSON."""
    match = re.search(r'"type"\s*:\s*"(chat|clarify)"', text)
    if not match:
        return None
    kind = match.group(1)
    content = re.search(r'"content"\s*:\s*(.+)', text[match.end():], re.DOTALL)
    if not content:
        return {"type": kind, "content": ""}
    raw = content.group(1).strip()
    # Trim the closing brace(s) that belong to the JSON envelope, keeping braces
    # that are part of the user-facing sentence.
    raw = re.sub(r'\s*,?\s*\}*\s*$', "", raw).strip()
    if raw.startswith('"'):
        try:
            value, _ = json.JSONDecoder().raw_decode(raw)
            if isinstance(value, str):
                return {"type": kind, "content": value}
        except json.JSONDecodeError:
            value = raw.strip('"')
            return {"type": kind, "content": value}
    if raw.endswith('"'):
        raw = raw[:-1]
    return {"type": kind, "content": raw.replace('\\"', '"').replace("\\n", "\n")}


def _candidates(text: str):
    """Yield every plausible JSON object embedded in a model reply."""
    decoder = json.JSONDecoder()
    for index, character in enumerate(text):
        if character != "{":
            continue
        try:
            candidate, _ = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(candidate, dict):
            yield candidate
    salvage = _salvage_text_object(text)
    if salvage is not None:
        yield salvage
    # Python-style dicts appear when a small model learned from Python prompts.
    for match in re.finditer(r"\{[^{}]*\}", text, re.DOTALL):
        try:
            candidate = ast.literal_eval(match.group(0))
        except (ValueError, SyntaxError):
            continue
        if isinstance(candidate, dict):
            yield candidate


def _strip_think_blocks(text: str) -> str:
    if "<think>" not in text.lower():
        return text
    cleaned = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL | re.IGNORECASE)
    return "" if "<think>" in cleaned.lower() else cleaned.strip()


def extract_reply_object(text: str) -> dict[str, Any] | None:
    """Return the first dict that looks like a HINATA reply, or ``None``."""
    cleaned = _strip_think_blocks(text)
    for candidate in _candidates(cleaned):
        kind = candidate.get("type")
        if kind in {"chat", "clarify"}:
            content = candidate.get("content")
            if isinstance(content, str) and content.strip():
                return candidate
            continue
        if kind in {"tool", "plan"}:
            return candidate
    return None


def normalize_plan(candidate: dict[str, Any]) -> dict[str, Any]:
    """Give plan steps a missing ``arguments`` key so policy validation decides."""
    steps = candidate.get("steps")
    if not isinstance(steps, list):
        return candidate
    normalized: list[Any] = []
    for step in steps:
        if isinstance(step, dict) and isinstance(step.get("tool"), str) and "arguments" not in step:
            extra = {key: value for key, value in step.items() if key != "tool"}
            step = {"tool": step["tool"], "arguments": extra}
        normalized.append(step)
    return {**candidate, "steps": normalized}


def _display_chat_text(candidate: dict[str, Any]) -> str | None:
    """Recover text from a readable, non-action JSON shape.

    Ollama's JSON mode guarantees syntax, not HINATA's response schema.  Small
    models commonly use ``response`` or an OpenAI-style nested ``message``
    instead.  These shapes are safe to display as chat, but anything resembling
    an action remains the caller's responsibility to validate or reject.
    """
    if candidate.get("type") in {"tool", "plan"} or ACTION_KEYS.intersection(candidate):
        return None
    for key in CHAT_TEXT_KEYS:
        value = candidate.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    nested = candidate.get("message")
    if isinstance(nested, dict) and not ACTION_KEYS.intersection(nested):
        for key in CHAT_TEXT_KEYS:
            value = nested.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    # Some small models return topic/fact JSON rather than the agreed chat shape.
    # Render it as display-only prose, never as a tool action.
    safe_fields = ("topic", "summary", "findings", "conclusion", "details", "description")
    rendered = [
        f"{key.replace('_', ' ').title()}: {value.strip()}"
        for key in safe_fields
        if isinstance((value := candidate.get(key)), str) and value.strip()
    ]
    if rendered:
        return "\n".join(rendered)
    return None


def _without_code_fence(text: str) -> str:
    """Remove one optional outer Markdown fence without eating inner fences."""
    stripped = text.strip()
    match = re.fullmatch(r"```[a-zA-Z0-9_-]*\s*\n?(.*?)\n?```", stripped, re.DOTALL)
    return match.group(1).strip() if match else stripped


def plain_text_reply(text: str) -> str | None:
    """Use prose or a readable non-action JSON reply as display-only chat."""
    stripped = _without_code_fence(_strip_think_blocks(text))
    if len(stripped) < 2:
        return None

    # A JSON string is another common small-model response to a greeting.
    try:
        decoded = json.loads(stripped)
    except json.JSONDecodeError:
        decoded = None
    if isinstance(decoded, str) and decoded.strip():
        return decoded.strip()

    for candidate in _candidates(stripped):
        salvaged = _display_chat_text(candidate)
        if salvaged:
            return salvaged

    # Do not show abandoned JSON/action machinery as if it were an assistant's
    # conversational answer.  Recognized actions are validated elsewhere.
    if stripped[0] in "{[" or stripped[-1] in "}]":
        return None
    if re.search(r'["\']?\s*(?:type|tool|arguments|objective|steps)\s*["\']?\s*:', stripped):
        return None
    return stripped
