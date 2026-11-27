"""Shared result types used by text, voice, and desktop front ends."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from hinata.security.redaction import redact_text, redact_value


@dataclass(frozen=True, slots=True)
class ActionRequest:
    tool: str
    arguments: dict[str, Any] = field(default_factory=dict)
    original_text: str = ""


@dataclass(slots=True)
class ActionResult:
    tool: str
    success: bool
    message: str
    data: dict[str, Any] = field(default_factory=dict)
    status: str = "completed"

    def __post_init__(self) -> None:
        # Last line of defence: every front end sees this object, and a tool can
        # surface a filename, URL or exception text containing a credential.
        self.message = redact_text(self.message)
        self.data = redact_value(self.data)

    @classmethod
    def failure(cls, tool: str, message: str, status: str = "failed") -> ActionResult:
        return cls(tool=tool, success=False, message=message, status=status)


@dataclass(slots=True)
class AssistantResponse:
    text: str
    success: bool = True
    status: str = "completed"
    action: ActionRequest | None = None
    data: dict[str, Any] = field(default_factory=dict)
    plan_actions: list[ActionRequest] = field(default_factory=list)

    def __post_init__(self) -> None:
        # A response can be built by any of a dozen orchestrator branches; apply
        # the secret policy once here so a future branch cannot bypass it. The
        # pending ActionRequest is deliberately not rewritten: confirmation and
        # resume must operate on the exact action the owner approved.
        self.text = redact_text(self.text)
        self.data = redact_value(self.data)

    @property
    def needs_plan_confirmation(self) -> bool:
        return self.status == "plan_confirmation_required"

    @property
    def needs_confirmation(self) -> bool:
        return self.status == "confirmation_required"

    @property
    def needs_pin(self) -> bool:
        return self.status == "pin_required"
