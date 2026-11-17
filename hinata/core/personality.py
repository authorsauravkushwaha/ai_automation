"""Configurable response styles and owner-name personalization without changing tool authority."""

from __future__ import annotations

import re
from dataclasses import dataclass

from hinata.database.database import Database
from hinata.security.redaction import contains_secrets


@dataclass(frozen=True, slots=True)
class PersonalityMode:
    key: str
    label: str
    instruction: str


MODES: dict[str, PersonalityMode] = {
    "friend": PersonalityMode(
        "friend",
        "Friend",
        "Speak in a soft, warm, gentle, kind, and supportive feminine persona like Hinata. Be patient, encouraging, and clear.",
    ),
    "advisor": PersonalityMode(
        "advisor", "Advisor", "Give practical, honest advice with a calm, respectful tone. Point out weak reasoning kindly; avoid empty praise.",
    ),
    "technical": PersonalityMode(
        "technical", "Technical", "Be a precise, fast technical assistant. Explain assumptions clearly, diagnose errors, and give actionable steps and clean code.",
    ),
    "strategist": PersonalityMode(
        "strategist", "Strategist", "Analyze objectives, trade-offs, and risks. Break complex goals into realistic, parallelizable steps.",
    ),
    "professional": PersonalityMode(
        "professional", "Professional", "Be concise, clear, polite, and well-structured.",
    ),
    "jarvis": PersonalityMode(
        "jarvis",
        "Jarvis Tactical",
        "Respond like an ultra-competent, calm, crisp, tactical AI chief-of-staff. "
        "Lead with situation status, telemetry, and exact execution options; stay articulate, loyal to owner authority, and zero-fluff.",
    ),
}


def parse_owner_names(raw: str) -> tuple[str, ...]:
    """Parse one or multiple owner names (e.g. 'Saurav', 'Saurav and Rahul', 'Saurav, Priya')."""
    cleaned = " ".join(raw.replace("\x00", "").strip().split())
    if not cleaned:
        return ()
    parts = re.split(r"\s*(?:,|&|\band\b)\s*", cleaned, flags=re.IGNORECASE)
    names: list[str] = []
    for part in parts:
        token = part.strip(" .,!?:;\"'")
        if token and token.casefold() not in {n.casefold() for n in names}:
            names.append(token[:60])
    return tuple(names[:6])


def format_owner_names(names: tuple[str, ...]) -> str:
    """Format parsed names naturally for speech and text greetings."""
    if not names:
        return ""
    if len(names) == 1:
        return names[0]
    if len(names) == 2:
        return f"{names[0]} and {names[1]}"
    return f"{', '.join(names[:-1])}, and {names[-1]}"


class PersonalityManager:
    """Owns the selected style and owner name(s); mode/name changes never grant permissions or alter policy."""

    def __init__(
        self,
        database: Database,
        default_mode: str = "friend",
        *,
        default_user_name: str = "",
    ) -> None:
        if default_mode not in MODES:
            raise ValueError(f"Unknown personality mode: {default_mode}")
        self.database = database
        stored = database.get_setting("personality.mode")
        self._mode = stored if stored in MODES else default_mode
        stored_name = database.get_setting("owner.name")
        initial_name = stored_name if stored_name is not None else default_user_name
        self._owner_names = parse_owner_names(initial_name or "")

    @property
    def mode(self) -> PersonalityMode:
        return MODES[self._mode]

    @property
    def choices(self) -> tuple[PersonalityMode, ...]:
        return tuple(MODES.values())

    @property
    def owner_names(self) -> tuple[str, ...]:
        return self._owner_names

    @property
    def owner_name(self) -> str:
        return format_owner_names(self._owner_names)

    @property
    def effective_instruction(self) -> str:
        base = self.mode.instruction
        who = self.owner_name
        if not who:
            return base
        plural_note = (
            f"The owners using this PC are {who}. Address them warmly and naturally by name when greeting or helping them."
            if len(self._owner_names) > 1
            else f"The user's name is {who}. Address {who} warmly, respectfully, and naturally by name in greetings and replies."
        )
        return f"{base} {plural_note}"

    def welcome_greeting(self) -> str:
        who = self.owner_name
        if who:
            return (
                f"Hello {who}! I'm Hinata, your personal AI assistant. "
                "I can search the internet, open your installed apps, write code in any language, "
                "and handle multiple tasks at once. How can I help you today?"
            )
        return (
            "Hello! I'm Hinata, your personal AI assistant. "
            "I can search the internet, open your installed apps, write code in any language, "
            "and handle multiple tasks at once. Tip: say 'My name is ...' or set your name in Settings so I can address you personally."
        )

    def voice_test_phrase(self) -> str:
        who = self.owner_name
        if who:
            return f"Hello {who}! I am Hinata. My voice and systems are ready to assist you."
        return "Hello! I am Hinata. My voice and systems are ready to assist you."

    def set_owner_name(self, raw_name: str) -> str:
        cleaned = raw_name.strip()
        if len(cleaned) > 160 or "\x00" in cleaned:
            raise ValueError("Name must be at most 160 characters")
        if cleaned and contains_secrets(cleaned):
            raise ValueError("Refusing to store secret-like text as an owner name")
        parsed = parse_owner_names(cleaned)
        formatted = format_owner_names(parsed)
        self.database.set_setting("owner.name", formatted)
        self._owner_names = parsed
        return formatted

    def set_mode(self, mode: str) -> PersonalityMode:
        normalized = mode.strip().lower()
        if normalized not in MODES:
            raise ValueError(f"Mode must be one of: {', '.join(MODES)}")
        self.database.set_setting("personality.mode", normalized)
        self._mode = normalized
        return self.mode
