"""Configurable response styles that shape tone without changing tool authority."""

from __future__ import annotations

from dataclasses import dataclass

from hinata.database.database import Database


@dataclass(frozen=True, slots=True)
class PersonalityMode:
    key: str
    label: str
    instruction: str


MODES: dict[str, PersonalityMode] = {
    "friend": PersonalityMode(
        "friend", "Friend", "Be warm, casual, patient, and thoughtful. Listen before advising.",
    ),
    "advisor": PersonalityMode(
        "advisor", "Advisor", "Give practical, honest advice. Point out weak reasoning respectfully; avoid empty praise.",
    ),
    "technical": PersonalityMode(
        "technical", "Technical", "Be a precise technical assistant. Explain assumptions, diagnose errors, and give actionable steps.",
    ),
    "strategist": PersonalityMode(
        "strategist", "Strategist", "Analyze objectives, trade-offs, and risks. Break complex goals into realistic steps.",
    ),
    "professional": PersonalityMode(
        "professional", "Professional", "Be concise, clear, formal, and well-structured.",
    ),
    "jarvis": PersonalityMode(
        "jarvis",
        "Jarvis Tactical",
        "Respond like an ultra-competent, calm, crisp, tactical AI chief-of-staff (J.A.R.V.I.S. style). "
        "Lead with situation status, telemetry, and exact execution options; stay dryly articulate, loyal to owner authority, and zero-fluff.",
    ),
}


class PersonalityManager:
    """Owns the selected style; mode changes never grant permissions or alter policy."""

    def __init__(self, database: Database, default_mode: str = "friend") -> None:
        if default_mode not in MODES:
            raise ValueError(f"Unknown personality mode: {default_mode}")
        self.database = database
        stored = database.get_setting("personality.mode")
        self._mode = stored if stored in MODES else default_mode

    @property
    def mode(self) -> PersonalityMode:
        return MODES[self._mode]

    @property
    def choices(self) -> tuple[PersonalityMode, ...]:
        return tuple(MODES.values())

    def set_mode(self, mode: str) -> PersonalityMode:
        normalized = mode.strip().lower()
        if normalized not in MODES:
            raise ValueError(f"Mode must be one of: {', '.join(MODES)}")
        self.database.set_setting("personality.mode", normalized)
        self._mode = normalized
        return self.mode
