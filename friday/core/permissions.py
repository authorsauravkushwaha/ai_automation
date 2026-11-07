"""Central authorization policy shared by every front end."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from friday.core.types import ActionRequest
from friday.security.authentication import Authentication


class PermissionLevel(StrEnum):
    SAFE = "safe"
    CONFIRM = "confirm"
    SENSITIVE = "sensitive"


POLICY = {
    "open_application": PermissionLevel.SAFE,
    "close_application": PermissionLevel.CONFIRM,
    "open_folder": PermissionLevel.SAFE,
    "open_file": PermissionLevel.CONFIRM,
    "search_files": PermissionLevel.SAFE,
    "create_folder": PermissionLevel.SAFE,
    "read_text_file": PermissionLevel.SAFE,
    "type_text": PermissionLevel.CONFIRM,
    "volume": PermissionLevel.SAFE,
    "system_info": PermissionLevel.SAFE,
    "memory_search": PermissionLevel.SAFE,
    "remember_preference": PermissionLevel.CONFIRM,
    "list_preferences": PermissionLevel.SAFE,
    "clear_memory": PermissionLevel.SENSITIVE,
}


@dataclass(slots=True)
class PermissionDecision:
    allowed: bool
    status: str
    message: str


class PermissionManager:
    def __init__(self, authentication: Authentication) -> None:
        self.authentication = authentication

    def level(self, tool: str) -> PermissionLevel:
        return POLICY.get(tool, PermissionLevel.SENSITIVE)

    def check(
        self,
        action: ActionRequest,
        *,
        source: str,
        speaker_match: bool | None,
        speaker_required: bool,
        confirmed: bool,
        pin: str | None,
    ) -> PermissionDecision:
        if source == "voice" and (speaker_match is False or (speaker_required and speaker_match is not True)):
            return PermissionDecision(
                False,
                "denied",
                "Speaker verification failed or was uncertain. Use typed input or re-enroll your voice.",
            )
        level = self.level(action.tool)
        if level is PermissionLevel.SAFE:
            return PermissionDecision(True, "allowed", "Allowed")
        if not confirmed:
            return PermissionDecision(
                False,
                "confirmation_required",
                f"'{action.tool}' needs explicit confirmation before it can run.",
            )
        if level is PermissionLevel.SENSITIVE:
            if not self.authentication.is_configured:
                return PermissionDecision(
                    False, "denied", "No FRIDAY PIN is configured. Run 'friday setup-pin' first."
                )
            if not pin:
                return PermissionDecision(
                    False, "pin_required", "Enter your FRIDAY PIN to authorize this sensitive action."
                )
            if not self.authentication.verify_pin(pin):
                return PermissionDecision(False, "denied", "The PIN is incorrect or has not been configured.")
        return PermissionDecision(True, "allowed", "Authorized")
