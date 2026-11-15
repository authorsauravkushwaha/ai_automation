"""Consistent, privacy-conscious spoken replies for typed and voice requests."""

from __future__ import annotations

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


def spoken_reply(response: AssistantResponse) -> str:
    """Never read PINs or approval arguments aloud; keep full details on screen."""
    if response.needs_pin:
        return "Your owner PIN is required. Enter it privately in the prompt on screen."
    if response.needs_plan_confirmation:
        return "I have prepared a plan. Please review its steps on screen and approve or decline."
    if response.needs_confirmation:
        return "This action requires your approval. Review the tool and arguments on screen first."
    text = response.text.strip()
    if len(text) > 4000:
        return text[:3900].rsplit(" ", 1)[0] + ". The full response is on screen."
    return text
