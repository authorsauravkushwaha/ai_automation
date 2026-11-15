"""Consistent, privacy-conscious spoken replies for typed and voice requests."""

from __future__ import annotations

from hinata.core.types import AssistantResponse


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
