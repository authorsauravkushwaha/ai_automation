"""Procedural Workflow Playbooks — promote verified DAG tasks into reusable offline templates.

Safety rules:
- Only ``COMPLETED`` / ``SUCCEEDED`` tasks (or explicitly validated step lists)
  can be promoted into a playbook.
- Instantiating a playbook NEVER executes steps directly: it runs every step
  through ``Planner.validate()`` (re-checking allowlists, DAG cycles, write
  conflicts, and parameter schemas) and creates a fresh ``AWAITING_APPROVAL``
  checkpoint bound to a new ``plan_hash``.
- Secret patterns are blocked from playbook parameters and step templates.
"""

from __future__ import annotations

import json
import re
import uuid
from typing import Any

from hinata.core.planner import ExecutionPlan, Planner, PlanningError
from hinata.database.database import Database
from hinata.security.redaction import contains_secrets
from hinata.tasks.store import TaskStateError, TaskStore

_PLACEHOLDER_RE = re.compile(r"\{\{([A-Za-z_][A-Za-z0-9_]{0,31})\}\}")
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_\-]{1,63}$")


def _substitute_value(val: Any, params: dict[str, str]) -> Any:
    if isinstance(val, str):
        def _repl(match: re.Match[str]) -> str:
            key = match.group(1)
            if key not in params:
                raise PlanningError(f"Missing required playbook parameter: '{key}'")
            return str(params[key])

        return _PLACEHOLDER_RE.sub(_repl, val)
    if isinstance(val, dict):
        return {k: _substitute_value(v, params) for k, v in val.items()}
    if isinstance(val, list):
        return [_substitute_value(item, params) for item in val]
    return val


class PlaybookManager:
    """Stores, parameterizes, and safely instantiates reusable procedural DAG playbooks."""

    def __init__(self, database: Database, planner: Planner, tasks: TaskStore) -> None:
        self.database = database
        self.database.ensure_migrated()
        self.planner = planner
        self.tasks = tasks

    @staticmethod
    def _validate_name(name: str) -> str:
        cleaned = name.strip()
        if not _NAME_RE.match(cleaned):
            raise ValueError(
                "Playbook name must be 2–64 alphanumeric/hyphen/underscore characters"
            )
        return cleaned

    def register_playbook(
        self,
        *,
        name: str,
        description: str,
        steps: list[dict[str, Any]],
        tags: list[str] | None = None,
        source_task_id: str = "",
    ) -> dict[str, Any]:
        pb_name = self._validate_name(name)
        if not description.strip() or len(description.strip()) > 240:
            raise ValueError("Playbook description must be 1–240 characters")
        serialized_steps = json.dumps(steps, ensure_ascii=False)
        if contains_secrets(serialized_steps) or contains_secrets(description):
            raise ValueError("Refusing to store detected secret material inside a playbook")

        # Discover placeholders so we can dry-run validate with safe dummy values
        placeholders = set(_PLACEHOLDER_RE.findall(serialized_steps))
        dummy_params = {p: f"sample_{p}" for p in placeholders}
        candidate = {
            "type": "plan",
            "objective": description.strip(),
            "steps": _substitute_value(steps, dummy_params),
        }
        validated = self.planner.validate(candidate, max_steps_override=32)

        existing = self.database.fetch_one(
            "SELECT playbook_id FROM procedural_playbooks WHERE name = ?", (pb_name,)
        )
        pb_id = str(existing["playbook_id"]) if existing else uuid.uuid4().hex[:12]
        clean_tags = [str(t).strip()[:32] for t in (tags or []) if str(t).strip()][:10]

        self.database.execute(
            """INSERT INTO procedural_playbooks(
                   playbook_id, name, description, steps_json, source_task_id, plan_hash, tags_json
               ) VALUES(?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(name) DO UPDATE SET
                   description = excluded.description,
                   steps_json = excluded.steps_json,
                   source_task_id = excluded.source_task_id,
                   plan_hash = excluded.plan_hash,
                   tags_json = excluded.tags_json,
                   updated_at = datetime('now')""",
            (
                pb_id,
                pb_name,
                description.strip(),
                serialized_steps,
                source_task_id,
                validated.plan_hash(),
                json.dumps(clean_tags, ensure_ascii=False),
            ),
        )
        return {
            "playbook_id": pb_id,
            "name": pb_name,
            "description": description.strip(),
            "step_count": len(steps),
            "parameters": sorted(placeholders),
            "plan_hash": validated.plan_hash(),
            "tags": clean_tags,
        }

    def promote_from_task(
        self,
        task_id: str,
        *,
        name: str,
        description: str = "",
        parameter_bindings: dict[str, str] | None = None,
        tags: list[str] | None = None,
    ) -> dict[str, Any]:
        """Promote a verified COMPLETED/SUCCEEDED task into a reusable playbook."""
        task = self.tasks.get(task_id)
        if task is None:
            raise TaskStateError(f"Task '{task_id}' not found")
        if task["state"] not in {"COMPLETED", "SUCCEEDED"}:
            raise TaskStateError(
                f"Only verified COMPLETED tasks can be promoted to playbooks (got '{task['state']}')"
            )
        raw_steps: list[dict[str, Any]] = []
        for row in task.get("steps", []):
            args = json.loads(str(row["arguments_json"]))
            if parameter_bindings:
                # Replace literal values matching parameter_bindings {param_name: literal_to_replace}
                for param_name, literal in parameter_bindings.items():
                    if not re.match(r"^[A-Za-z_][A-Za-z0-9_]{0,31}$", param_name) or not literal:
                        continue
                    args = json.loads(
                        json.dumps(args).replace(literal, f"{{{{{param_name}}}}}")
                    )
            step_entry: dict[str, Any] = {"tool": str(row["tool"]), "arguments": args}
            if row.get("step_id"):
                step_entry["step_id"] = str(row["step_id"])
            deps = json.loads(str(row.get("depends_on_json") or "[]"))
            if deps:
                step_entry["depends_on"] = deps
            raw_steps.append(step_entry)

        desc = description.strip() or str(task["objective"])[:240]
        return self.register_playbook(
            name=name,
            description=desc,
            steps=raw_steps,
            tags=tags,
            source_task_id=task_id,
        )

    def list_playbooks(self) -> list[dict[str, Any]]:
        rows = self.database.fetch_all(
            "SELECT * FROM procedural_playbooks ORDER BY use_count DESC, name ASC"
        )
        out: list[dict[str, Any]] = []
        for r in rows:
            placeholders = sorted(set(_PLACEHOLDER_RE.findall(str(r["steps_json"]))))
            out.append(
                {
                    "playbook_id": r["playbook_id"],
                    "name": r["name"],
                    "description": r["description"],
                    "parameters": placeholders,
                    "source_task_id": r["source_task_id"],
                    "use_count": r["use_count"],
                    "tags": json.loads(str(r["tags_json"] or "[]")),
                    "updated_at": r["updated_at"],
                }
            )
        return out

    def get_playbook(self, identifier: str) -> dict[str, Any] | None:
        row = self.database.fetch_one(
            "SELECT * FROM procedural_playbooks WHERE playbook_id = ? OR name = ?",
            (identifier.strip(), identifier.strip()),
        )
        if row is None:
            return None
        row["steps"] = json.loads(str(row["steps_json"]))
        row["tags"] = json.loads(str(row["tags_json"] or "[]"))
        row["parameters"] = sorted(set(_PLACEHOLDER_RE.findall(str(row["steps_json"]))))
        return row

    def delete_playbook(self, identifier: str) -> bool:
        with self.database.connect() as conn:
            res = conn.execute(
                "DELETE FROM procedural_playbooks WHERE playbook_id = ? OR name = ?",
                (identifier.strip(), identifier.strip()),
            )
            return (res.rowcount or 0) > 0

    def instantiate(
        self,
        identifier: str,
        *,
        params: dict[str, str] | None = None,
        source: str = "text",
        autonomy_mode: str = "APPROVED_PLAN",
    ) -> tuple[str, ExecutionPlan]:
        """Bind parameters, validate the resulting DAG, and stage an AWAITING_APPROVAL task."""
        pb = self.get_playbook(identifier)
        if pb is None:
            raise ValueError(f"Playbook '{identifier}' not found")
        bound_params = dict(params or {})
        for v in bound_params.values():
            if contains_secrets(str(v)):
                raise ValueError("Refusing to bind secret material into playbook arguments")

        substituted_steps = _substitute_value(pb["steps"], bound_params)
        user_req = f"playbook:{pb['name']} " + json.dumps(bound_params, sort_keys=True)
        plan = self.planner.validate(
            {
                "type": "plan",
                "objective": str(pb["description"]),
                "steps": substituted_steps,
            },
            max_steps_override=32,
            user_request=user_req,
            autonomy_mode=autonomy_mode,
        )
        task_id = self.tasks.create(
            plan,
            source,
            user_request=user_req,
            autonomy_mode=autonomy_mode,
        )
        self.database.execute(
            "UPDATE procedural_playbooks SET use_count = use_count + 1, updated_at = datetime('now') WHERE playbook_id = ?",
            (pb["playbook_id"],),
        )
        return task_id, plan
