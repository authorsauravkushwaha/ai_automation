"""Independent, read-only postcondition checks for local actions and patches."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from hinata.automation.app_control import AutomationError
from hinata.automation.file_manager import FileManager
from hinata.core.types import ActionRequest
from hinata.database.database import Database


@dataclass(frozen=True, slots=True)
class Check:
    step: int
    tool: str
    status: str  # verified, contradicted, or unknown
    detail: str


class ReviewAgent:
    """Checks specific read-only postconditions; cannot infer arbitrary objective success."""

    def __init__(self, files: FileManager, database: Database | None = None) -> None:
        self.files = files
        self.database = database

    def review(self, steps: tuple[ActionRequest, ...]) -> list[Check]:
        checks: list[Check] = []
        for index, action in enumerate(steps, 1):
            if action.tool == "create_folder":
                try:
                    exists = self.files.resolve(action.arguments["path"]).is_dir()
                except (AutomationError, OSError):
                    exists = False
                checks.append(
                    Check(
                        index,
                        action.tool,
                        "verified" if exists else "contradicted",
                        "Folder exists in an allowed root" if exists else "Folder is missing or inaccessible",
                    )
                )
            elif action.tool in {"write_text_file", "copy_file", "move_file"}:
                target_key = "path" if action.tool == "write_text_file" else "destination"
                try:
                    exists = self.files.resolve(action.arguments[target_key]).is_file()
                except (AutomationError, OSError):
                    exists = False
                checks.append(
                    Check(
                        index,
                        action.tool,
                        "verified" if exists else "contradicted",
                        "Target file exists in permitted root" if exists else "Target file missing after write/copy/move",
                    )
                )
            elif action.tool == "archive_files":
                try:
                    exists = self.files.resolve(action.arguments["archive_path"]).is_file()
                except (AutomationError, OSError):
                    exists = False
                checks.append(
                    Check(
                        index,
                        action.tool,
                        "verified" if exists else "contradicted",
                        "Archive file exists" if exists else "Archive file missing",
                    )
                )
            elif action.tool == "workspace_apply_patch" and self.database is not None:
                patch_id = action.arguments.get("patch_id", "")
                row = self.database.fetch_one("SELECT state FROM coding_patches WHERE patch_id = ?", (patch_id,))
                ok = row is not None and row["state"] == "APPLIED"
                checks.append(
                    Check(
                        index,
                        action.tool,
                        "verified" if ok else "contradicted",
                        f"Patch {patch_id} state is APPLIED" if ok else f"Patch {patch_id} is not in APPLIED state",
                    )
                )
            elif action.tool == "workspace_revert_patch" and self.database is not None:
                patch_id = action.arguments.get("patch_id", "")
                row = self.database.fetch_one("SELECT state FROM coding_patches WHERE patch_id = ?", (patch_id,))
                ok = row is not None and row["state"] == "REVERTED"
                checks.append(
                    Check(
                        index,
                        action.tool,
                        "verified" if ok else "contradicted",
                        f"Patch {patch_id} reverted cleanly" if ok else f"Patch {patch_id} revert could not be verified",
                    )
                )
            else:
                checks.append(Check(index, action.tool, "unknown", "No independent postcondition check is available"))
        return checks

    def reconcile_single_effect(self, tool: str, arguments: dict[str, Any]) -> tuple[str, str]:
        """Read-only reconciliation probe used after crash interruption. Never executes side effects."""
        if tool == "create_folder":
            try:
                p = self.files.resolve(arguments["path"], must_exist=False)
                if p.is_dir():
                    return "verified", f"Folder {p} exists on disk"
                return "not_applied", f"Folder {p} does not exist on disk"
            except (AutomationError, OSError) as exc:
                return "unknown", str(exc)
        if tool == "write_text_file":
            try:
                p = self.files.resolve(arguments["path"], must_exist=False)
                if not p.exists():
                    return "not_applied", f"File {p} was not created"
                actual = self.files.read_text(str(p), max_chars=100_000)["content"]
                if actual == arguments.get("content", ""):
                    return "verified", f"File {p} matches expected written content"
                return "unknown", f"File {p} exists with differing content"
            except (AutomationError, OSError) as exc:
                return "unknown", str(exc)
        if tool == "workspace_apply_patch" and self.database is not None:
            row = self.database.fetch_one(
                "SELECT state FROM coding_patches WHERE patch_id = ?", (arguments.get("patch_id", ""),)
            )
            if row and row["state"] == "APPLIED":
                return "verified", "Patch state recorded as APPLIED"
            if row and row["state"] == "PROPOSED":
                return "not_applied", "Patch remains in PROPOSED state"
        return "unknown", "Automatic side-effect reconciliation is unavailable for this tool"

    def verify_objective(self, request: str, steps: tuple[ActionRequest, ...]) -> dict | None:
        """Verify only an unambiguous exact folder-existence request or list.

        Arbitrary prose, multi-step plans, and alternate targets remain unknown.
        This result is based on a fresh filesystem check, not the model's claim.
        """
        match = re.fullmatch(r"ensure folder (\S{1,1024}) exists", request.strip(), re.IGNORECASE)
        plural = re.fullmatch(r"ensure folders ([^\s,]+(?:, [^\s,]+){1,4}) exist", request.strip(), re.IGNORECASE)
        if match:
            targets = [match.group(1)]
        elif plural:
            targets = plural.group(1).split(", ")
        else:
            return None
        if len(steps) != len(targets) or any(step.tool != "create_folder" for step in steps):
            return None
        try:
            requested = [self.files.resolve(target) for target in targets]
            executed = [self.files.resolve(step.arguments["path"]) for step in steps]
            # A one-to-one match prevents duplicate steps or aliasing paths from
            # accidentally proving a different objective.
            if (len(set(requested)) != len(requested) or len(set(executed)) != len(executed)
                    or set(requested) != set(executed) or not all(path.is_dir() for path in requested)):
                return None
        except (AutomationError, OSError):
            return None
        if match:
            return {"kind": "folder_exists", "path": str(requested[0]),
                    "requested": request, "tool_path": steps[0].arguments["path"]}
        return {"kind": "folders_exist", "paths": [str(path) for path in requested],
                "requested": request, "tool_paths": [step.arguments["path"] for step in steps]}
