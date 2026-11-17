"""Consistent, privacy-conscious, gentle spoken replies for typed and voice requests."""

from __future__ import annotations

import re
from typing import Protocol

from hinata.config import InterfaceConfig
from hinata.core.types import AssistantResponse


class SpeechSettingStore(Protocol):
    def get_setting(self, key: str) -> str | None: ...


def speaking_enabled(config: InterfaceConfig, settings: SpeechSettingStore) -> bool:
    """A saved user choice overrides the shipped default and legacy config."""
    stored = settings.get_setting("ui.speak_responses")
    if stored in {"true", "false"}:
        return stored == "true"
    return config.speak_responses


def clean_for_speech(text: str) -> str:
    """Soften code blocks, raw URLs, and heavy markdown so Hinata speaks smoothly while preserving plain text."""
    stripped = text.strip()
    if not stripped:
        return ""
    # Fast-path: leave ordinary plain-text sentences untouched byte-for-byte.
    if "```" not in stripped and "http://" not in stripped and "https://" not in stripped and "**" not in stripped and not stripped.startswith("#"):
        return stripped

    cleaned = re.sub(
        r"```[a-zA-Z0-9_+#.-]*\s*\n.*?\n?```",
        " I have written the code on screen for you. ",
        stripped,
        flags=re.DOTALL,
    )
    cleaned = re.sub(r"\[([^\]]+)\]\(https?://[^\s)]+\)", r"\1", cleaned)
    cleaned = re.sub(r"https?://\S+", "", cleaned)
    cleaned = re.sub(r"(?m)^#{1,6}\s+", "", cleaned)
    cleaned = cleaned.replace("**", "").replace("__", "").replace("`", "")
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned).strip()
    return cleaned or "The response is ready on screen."


def spoken_reply(response: AssistantResponse) -> str:
    """Never read PINs or approval arguments aloud; keep full details on screen."""
    if response.needs_pin:
        return "Your owner PIN is required. Enter it privately in the prompt on screen."
    if response.needs_plan_confirmation:
        return "I have prepared a plan. Please review its steps on screen and approve or decline."
    if response.needs_confirmation:
        return "This action requires your approval. Review the tool and arguments on screen first."
    text = clean_for_speech(response.text)
    if len(text) > 4000:
        return text[:3900].rsplit(" ", 1)[0] + ". The full response is on screen."
    return text
