"""Independent, read-only postcondition checks for a limited tool subset."""
from __future__ import annotations

import re
from dataclasses import dataclass

from hinata.automation.app_control import AutomationError
from hinata.automation.file_manager import FileManager
from hinata.core.types import ActionRequest


@dataclass(frozen=True, slots=True)
class Check:
    step: int
    tool: str
    status: str  # verified, contradicted, or unknown
    detail: str


class ReviewAgent:
    """Checks specific postconditions; cannot infer arbitrary objective success."""

    def __init__(self, files: FileManager) -> None:
        self.files = files

    def review(self, steps: tuple[ActionRequest, ...]) -> list[Check]:
        checks = []
        for index, action in enumerate(steps, 1):
            if action.tool == "create_folder":
                try:
                    exists = self.files.resolve(action.arguments["path"]).is_dir()
                except (AutomationError, OSError):
                    exists = False
                checks.append(Check(index, action.tool, "verified" if exists else "contradicted",
                                    "Folder exists in an allowed root" if exists else "Folder is missing or inaccessible"))
            else:
                checks.append(Check(index, action.tool, "unknown", "No independent postcondition check is available"))
        return checks

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
