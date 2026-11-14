"""A bounded, proposal-only planning worker; cannot execute or approve tools."""
from __future__ import annotations

import json

from hinata.core.intent_parser import IntentParser
from hinata.core.llm_manager import LLMManager
from hinata.core.planner import ExecutionPlan, Planner, PlanningError
from hinata.core.types import ActionRequest
from hinata.security.action_validator import ActionValidator


class Replanner:
    def __init__(self, llm: LLMManager, parser: IntentParser, planner: Planner, validator: ActionValidator) -> None:
        self.llm = llm
        self.parser = parser
        self.planner = planner
        self.validator = validator

    def propose(self, objective: str, failed_tool: str, attempted: list[ActionRequest]) -> ExecutionPlan:
        # Do not send tool output or exception content to the model. Objective and
        # arguments are data and never gain authority over validation or approval.
        facts = json.dumps({"objective": objective, "failed_tool": failed_tool,
                            "attempted": [{"tool": step.tool, "arguments": step.arguments} for step in attempted]},
                           ensure_ascii=True)
        request = ("The approved plan stopped because one tool failed. Propose a NEW plan for this objective. "
                   "Do not repeat any attempted tool with the same arguments. "
                   "If no safe alternative is clear, return a clarification. Context: " + facts)
        raw = self.llm.complete(request, [], [], self.validator.tool_descriptions(),
                                "Propose only a short safe plan; never claim an action ran.",
                                connect=True, pull_missing=False)
        kind, payload = self.parser.parse_llm_plan(raw, request)
        if kind != "plan":
            raise PlanningError("No revised plan was proposed")
        plan = self.planner.validate(payload)
        attempted_signatures = {(step.tool, json.dumps(step.arguments, sort_keys=True)) for step in attempted}
        if any((step.tool, json.dumps(step.arguments, sort_keys=True)) in attempted_signatures for step in plan.steps):
            raise PlanningError("Revised plan repeats an attempted action")
        return plan
