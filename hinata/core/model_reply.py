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


def extract_reply_object(text: str) -> dict[str, Any] | None:
    """Return the first dict that looks like a HINATA reply, or ``None``."""
    for candidate in _candidates(text):
        if candidate.get("type") in {"tool", "plan", "chat", "clarify"}:
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


def plain_text_reply(text: str) -> str | None:
    """Use a model's non-JSON prose as an answer instead of discarding it."""
    stripped = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.MULTILINE).strip()
    if not stripped or stripped.startswith("{") or stripped.endswith("}"):
        return None
    if len(stripped) < 2:
        return None
    return stripped
