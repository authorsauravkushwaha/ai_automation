"""Shared result types used by text, voice, and desktop front ends."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


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

    @property
    def needs_plan_confirmation(self) -> bool:
        return self.status == "plan_confirmation_required"

    @property
    def needs_confirmation(self) -> bool:
        return self.status == "confirmation_required"

    @property
    def needs_pin(self) -> bool:
        return self.status == "pin_required"
