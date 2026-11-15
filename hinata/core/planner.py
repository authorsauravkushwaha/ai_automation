"""Graph-validated planner, objective contracts, and autonomy policies."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from typing import Any

from hinata.core.tool_registry import DEFAULT_MANIFESTS, ToolManifest
from hinata.core.types import ActionRequest
from hinata.security.action_validator import ActionValidator, ValidationError


class PlanningError(ValueError):
    """Raised when a proposed plan is malformed, cyclic, conflicting, or outside policy."""


# Safe low-impact tools permitted inside batch approved plans and low-risk autonomy.
# Confirmation/PIN-gated actions remain individually authorized unless explicitly
# approved via step-level approval mode.
PLAN_SAFE_TOOLS = frozenset(
    {
        "code_inspect",
        "inspect_repository_imports",
        "open_application",
        "open_folder",
        "list_directory",
        "search_files",
        "search_file_content",
        "create_folder",
        "read_text_file",
        "compare_files",
        "volume",
        "system_info",
        "hardware_profile",
        "memory_search",
        "list_preferences",
        "set_personality_mode",
        "knowledge_list",
        "knowledge_search",
        "knowledge_semantic_search",
        "knowledge_hybrid_search",
        "workspace_list",
        "workspace_read",
        "workspace_search",
        "workspace_diff",
        "workspace_propose_patch",
        "workspace_symbol_index",
        "run_linter",
        "run_type_checker",
    }
)
MAX_PLAN_STEPS = 5  # Default cap for standard single-shot plans; configurable up to 32 in Planner.
AUTONOMY_MODES = frozenset(
    {
        "SUGGEST_ONLY",
        "APPROVAL_EACH_STEP",
        "APPROVED_PLAN",
        "LOW_RISK_AUTONOMY",
        "OFFLINE_AUTONOMY",
    }
)

_MANIFEST_MAP: dict[str, ToolManifest] = {m.name: m for m in DEFAULT_MANIFESTS}


@dataclass(frozen=True, slots=True)
class ObjectiveContract:
    user_request: str
    objective: str
    acceptance_criteria: tuple[str, ...] = ()
    estimated_complexity: str = "low"  # low, medium, high
    requires_specialist: tuple[str, ...] = ()
    autonomy_mode: str = "APPROVED_PLAN"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class PlanNode:
    step_id: str
    action: ActionRequest
    agent_id: str = "execution"
    depends_on: tuple[str, ...] = ()
    preconditions: tuple[str, ...] = ()
    postconditions: tuple[str, ...] = ()
    side_effect: str = "read_only"
    risk_tier: str = "low"
    idempotency: str = "idempotent"
    timeout_seconds: float = 30.0
    max_retries: int = 0
    compensation_tool: str = ""
    approval_required: bool = True
    explanation: str = ""
    condition: str = ""  # Typed verified-observation condition (e.g. "step_1.succeeded")


@dataclass(frozen=True, slots=True)
class ExecutionPlan:
    objective: str
    steps: tuple[ActionRequest, ...]
    nodes: tuple[PlanNode, ...] = ()
    contract: ObjectiveContract | None = None

    def plan_hash(self) -> str:
        canonical = {
            "steps": [
                {
                    "step_id": n.step_id,
                    "tool": n.action.tool,
                    "arguments": n.action.arguments,
                    "depends_on": list(n.depends_on),
                    "condition": n.condition,
                }
                for n in (
                    self.nodes
                    if self.nodes
                    else tuple(
                        PlanNode(step_id=f"step_{i}", action=s)
                        for i, s in enumerate(self.steps, 1)
                    )
                )
            ],
        }
        encoded = json.dumps(canonical, sort_keys=True, ensure_ascii=True).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def topological_batches(self) -> list[list[PlanNode]]:
        """Return dependency-ordered execution waves; mutating steps stay serialized."""
        effective_nodes = (
            list(self.nodes)
            if self.nodes
            else [PlanNode(step_id=f"step_{i}", action=s) for i, s in enumerate(self.steps, 1)]
        )
        remaining = {n.step_id: n for n in effective_nodes}
        completed: set[str] = set()
        waves: list[list[PlanNode]] = []
        while remaining:
            ready = [
                n for n in effective_nodes
                if n.step_id in remaining and all(dep in completed for dep in n.depends_on)
            ]
            if not ready:
                raise PlanningError("Plan dependency graph contains an unresolvable cycle")
            # Parallelize only independent read_only steps; serialize any mutating step.
            readonly_ready = [n for n in ready if n.side_effect == "read_only"]
            if len(readonly_ready) > 1:
                batch = readonly_ready
            else:
                batch = [ready[0]]
            waves.append(batch)
            for node in batch:
                completed.add(node.step_id)
                remaining.pop(node.step_id, None)
        return waves

    def preview(self) -> str:
        rows = [f"Proposed plan: {self.objective} [hash:{self.plan_hash()[:12]}]"]
        if self.nodes:
            for index, node in enumerate(self.nodes, 1):
                deps = f" (after {', '.join(node.depends_on)})" if node.depends_on else ""
                meta = f"[{node.risk_tier}/{node.side_effect}]"
                rows.append(
                    f"{index}. ({node.step_id}) {node.action.tool.replace('_', ' ')} {meta}{deps} — {self._safe_summary(node.action)}"
                )
        else:
            rows.extend(
                f"{index}. {step.tool.replace('_', ' ')} — {self._safe_summary(step)}"
                for index, step in enumerate(self.steps, 1)
            )
        rows.append("No steps have run. Approving authorizes only these listed safe tools.")
        return "\n".join(rows)

    @staticmethod
    def _safe_summary(step: ActionRequest) -> str:
        # Show every exact argument; otherwise approval can hide a root or volume value.
        return json.dumps(step.arguments, ensure_ascii=True, sort_keys=True)


class Planner:
    """Validates model-produced step lists and DAG plans before showing an approval preview."""

    ALLOWED_STEP_KEYS = frozenset(
        {"tool", "arguments", "step_id", "depends_on", "agent_id", "explanation", "condition"}
    )
    ALLOWED_CONDITION_RE = r"^step_[A-Za-z0-9_]{1,24}\.(succeeded|verified)$"

    def __init__(
        self,
        validator: ActionValidator,
        *,
        max_steps: int = MAX_PLAN_STEPS,
    ) -> None:
        self.validator = validator
        self.max_steps = max(1, min(max_steps, 32))

    @staticmethod
    def build_contract(user_request: str, objective: str, steps: tuple[ActionRequest, ...], autonomy_mode: str = "APPROVED_PLAN") -> ObjectiveContract:
        specialists: list[str] = []
        criteria: list[str] = []
        for step in steps:
            if step.tool == "create_folder":
                criteria.append(f"Folder '{step.arguments.get('path', '')}' exists inside permitted root")
            elif step.tool.startswith("workspace_") or step.tool in {"code_inspect", "run_project_tests", "run_linter"}:
                if "coding" not in specialists:
                    specialists.append("coding")
                criteria.append(f"Coding operation '{step.tool}' verified")
            elif step.tool.startswith("knowledge_") or step.tool in {"search_files", "search_file_content"}:
                if "research" not in specialists:
                    specialists.append("research")
        complexity = "low" if len(steps) <= 2 else ("medium" if len(steps) <= 5 else "high")
        if not criteria:
            criteria.append("All approved steps succeed and independent postconditions are reviewed")
        return ObjectiveContract(
            user_request=user_request[:4000],
            objective=objective,
            acceptance_criteria=tuple(criteria[:10]),
            estimated_complexity=complexity,
            requires_specialist=tuple(specialists),
            autonomy_mode=autonomy_mode if autonomy_mode in AUTONOMY_MODES else "APPROVED_PLAN",
        )

    def validate(
        self,
        candidate: dict[str, Any],
        *,
        max_steps_override: int | None = None,
        allow_confirm_tools: bool = False,
        user_request: str = "",
        autonomy_mode: str = "APPROVED_PLAN",
    ) -> ExecutionPlan:
        if not isinstance(candidate, dict):
            raise PlanningError("A plan must be a JSON object")
        allowed_top = {"type", "objective", "steps"}
        if not set(candidate).issubset(allowed_top) or {"type", "objective", "steps"} - set(candidate):
            raise PlanningError("A plan must contain only type, objective, and steps")
        objective = candidate.get("objective")
        raw_steps = candidate.get("steps")
        if candidate.get("type") != "plan":
            raise PlanningError("The model response is not a plan")
        if not isinstance(objective, str) or not objective.strip() or len(objective.strip()) > 240:
            raise PlanningError("Plan objective must be a non-empty string of at most 240 characters")
        step_cap = max_steps_override if max_steps_override is not None else self.max_steps
        if not isinstance(raw_steps, list) or not 1 <= len(raw_steps) <= step_cap:
            raise PlanningError(f"A plan must contain between 1 and {step_cap} steps")

        steps: list[ActionRequest] = []
        nodes: list[PlanNode] = []
        seen_ids: set[str] = set()
        raw_deps_map: dict[str, tuple[str, ...]] = {}

        import re

        for index, raw_step in enumerate(raw_steps, 1):
            if not isinstance(raw_step, dict):
                raise PlanningError(f"Plan step {index} must be an object")
            unknown_keys = set(raw_step) - self.ALLOWED_STEP_KEYS
            if unknown_keys or "tool" not in raw_step or "arguments" not in raw_step:
                raise PlanningError(f"Plan step {index} has missing or unsupported keys")
            tool = raw_step["tool"]
            if not isinstance(tool, str):
                raise PlanningError(f"Plan step {index} tool must be a string")
            if not allow_confirm_tools and tool not in PLAN_SAFE_TOOLS:
                raise PlanningError(f"Plan step {index} uses a tool that requires individual approval")
            try:
                validated_action = self.validator.validate(tool, raw_step["arguments"])
            except ValidationError as exc:
                raise PlanningError(f"Plan step {index} is invalid: {exc}") from exc

            raw_id = raw_step.get("step_id", f"step_{index}")
            if not isinstance(raw_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", raw_id):
                raise PlanningError(f"Plan step {index} has an invalid step_id")
            if raw_id in seen_ids:
                raise PlanningError(f"Duplicate step_id '{raw_id}' in plan")
            seen_ids.add(raw_id)

            deps_raw = raw_step.get("depends_on", [] if "step_id" in raw_step else ([f"step_{index - 1}"] if index > 1 else []))
            if not isinstance(deps_raw, list) or any(not isinstance(d, str) for d in deps_raw):
                raise PlanningError(f"Plan step {index} depends_on must be a list of step_ids")
            if len(set(deps_raw)) != len(deps_raw) or raw_id in deps_raw:
                raise PlanningError(f"Plan step {index} has duplicate or self-referential dependencies")
            raw_deps_map[raw_id] = tuple(deps_raw)

            condition = str(raw_step.get("condition", "")).strip()
            if condition and not re.fullmatch(self.ALLOWED_CONDITION_RE, condition):
                raise PlanningError(f"Plan step {index} has an unsupported branch condition '{condition}'")

            manifest = _MANIFEST_MAP.get(tool)
            agent_id = str(raw_step.get("agent_id", "")).strip() or self._default_agent_for_tool(tool)
            explanation = str(raw_step.get("explanation", "")).strip()[:240]

            steps.append(validated_action)
            nodes.append(
                PlanNode(
                    step_id=raw_id,
                    action=validated_action,
                    agent_id=agent_id,
                    depends_on=tuple(deps_raw),
                    postconditions=("folder_exists",) if tool == "create_folder" else (),
                    side_effect=manifest.side_effect if manifest else "unknown",
                    risk_tier=manifest.risk_tier if manifest else "medium",
                    idempotency=manifest.idempotency if manifest else "non_idempotent",
                    timeout_seconds=manifest.timeout_seconds if manifest else 30.0,
                    max_retries=1 if (manifest and manifest.retryable and manifest.idempotency == "idempotent") else 0,
                    compensation_tool=manifest.compensation_tool if manifest else "",
                    approval_required=autonomy_mode not in {"LOW_RISK_AUTONOMY", "OFFLINE_AUTONOMY"},
                    explanation=explanation,
                    condition=condition,
                )
            )

        # Verify all dependency IDs exist and graph is acyclic (DAG).
        for step_id, deps in raw_deps_map.items():
            for dep in deps:
                if dep not in seen_ids:
                    raise PlanningError(f"Step '{step_id}' depends on unknown step '{dep}'")

        plan = ExecutionPlan(
            objective=objective.strip(),
            steps=tuple(steps),
            nodes=tuple(nodes),
            contract=self.build_contract(user_request or objective.strip(), objective.strip(), tuple(steps), autonomy_mode),
        )
        # Validate DAG acyclicity and detect conflicting parallel writes.
        _ = plan.topological_batches()
        self._check_write_conflicts(nodes)
        return plan

    @staticmethod
    def _check_write_conflicts(nodes: list[PlanNode]) -> None:
        """Detect independent steps writing to the same target path without a dependency ordering."""
        write_targets: list[tuple[PlanNode, str]] = []
        for node in nodes:
            if node.side_effect in {"local_write", "destructive"}:
                target = str(
                    node.action.arguments.get("path")
                    or node.action.arguments.get("destination")
                    or node.action.arguments.get("archive_path")
                    or ""
                ).strip().casefold()
                if target:
                    write_targets.append((node, target))

        # Transitive reachability helper.
        dep_lookup = {n.step_id: set(n.depends_on) for n in nodes}

        def _reaches(src: str, dst: str, visited: set[str] | None = None) -> bool:
            seen = visited or set()
            if dst in dep_lookup.get(src, set()):
                return True
            for parent in dep_lookup.get(src, set()):
                if parent not in seen:
                    seen.add(parent)
                    if _reaches(parent, dst, seen):
                        return True
            return False

        for i, (n1, t1) in enumerate(write_targets):
            for n2, t2 in write_targets[i + 1 :]:
                if t1 == t2 and not (_reaches(n1.step_id, n2.step_id) or _reaches(n2.step_id, n1.step_id)):
                    raise PlanningError(
                        f"Conflicting unordered writes to '{t1}' between '{n1.step_id}' and '{n2.step_id}'"
                    )

    @staticmethod
    def _default_agent_for_tool(tool: str) -> str:
        if tool.startswith("workspace_") or tool in {"code_inspect", "inspect_repository_imports", "run_project_tests", "run_linter", "run_type_checker"}:
            return "coding"
        if tool.startswith("knowledge_") or tool in {"search_files", "search_file_content", "list_directory"}:
            return "research"
        if tool in {"memory_search", "remember_preference", "list_preferences", "clear_memory"}:
            return "memory"
        if tool in {"system_info", "hardware_profile"}:
            return "system_monitor"
        return "execution"
