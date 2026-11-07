"""Conservative validator for short, owner-approved multi-action plans."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from hinata.core.types import ActionRequest
from hinata.security.action_validator import ActionValidator, ValidationError


class PlanningError(ValueError):
    """Raised when a proposed plan is malformed or outside the safe plan subset."""


# Plans may automate only low-impact tools. Confirmation/PIN-gated actions remain
# available as individual requests and cannot be smuggled into a multi-step plan.
PLAN_SAFE_TOOLS = frozenset(
    {
        "open_application",
        "open_folder",
        "search_files",
        "create_folder",
        "read_text_file",
        "volume",
        "system_info",
        "memory_search",
        "list_preferences",
        "set_personality_mode",
    }
)
MAX_PLAN_STEPS = 5


@dataclass(frozen=True, slots=True)
class ExecutionPlan:
    objective: str
    steps: tuple[ActionRequest, ...]

    def preview(self) -> str:
        rows = [f"Proposed plan: {self.objective}"]
        rows.extend(
            f"{index}. {step.tool.replace('_', ' ')} — {self._safe_summary(step)}"
            for index, step in enumerate(self.steps, 1)
        )
        rows.append("No steps have run. Approving authorizes only these listed safe tools.")
        return "\n".join(rows)

    @staticmethod
    def _safe_summary(step: ActionRequest) -> str:
        arguments = step.arguments
        if "path" in arguments:
            return str(arguments["path"])
        if "query" in arguments:
            return str(arguments["query"])
        if "name" in arguments:
            return str(arguments["name"])
        if "mode" in arguments:
            return str(arguments["mode"])
        if "action" in arguments:
            return str(arguments["action"])
        return "no arguments"


class Planner:
    """Validates model-produced steps before showing an approval preview."""

    def __init__(self, validator: ActionValidator) -> None:
        self.validator = validator

    def validate(self, candidate: dict[str, Any]) -> ExecutionPlan:
        if not isinstance(candidate, dict) or set(candidate) != {"type", "objective", "steps"}:
            raise PlanningError("A plan must contain only type, objective, and steps")
        objective = candidate.get("objective")
        raw_steps = candidate.get("steps")
        if candidate.get("type") != "plan":
            raise PlanningError("The model response is not a plan")
        if not isinstance(objective, str) or not objective.strip() or len(objective.strip()) > 240:
            raise PlanningError("Plan objective must be a non-empty string of at most 240 characters")
        if not isinstance(raw_steps, list) or not 1 <= len(raw_steps) <= MAX_PLAN_STEPS:
            raise PlanningError(f"A plan must contain between 1 and {MAX_PLAN_STEPS} steps")

        steps: list[ActionRequest] = []
        for index, raw_step in enumerate(raw_steps, 1):
            if not isinstance(raw_step, dict) or set(raw_step) != {"tool", "arguments"}:
                raise PlanningError(f"Plan step {index} must contain only tool and arguments")
            tool = raw_step["tool"]
            if not isinstance(tool, str) or tool not in PLAN_SAFE_TOOLS:
                raise PlanningError(f"Plan step {index} uses a tool that requires individual approval")
            try:
                steps.append(self.validator.validate(tool, raw_step["arguments"]))
            except ValidationError as exc:
                raise PlanningError(f"Plan step {index} is invalid: {exc}") from exc
        return ExecutionPlan(objective.strip(), tuple(steps))
