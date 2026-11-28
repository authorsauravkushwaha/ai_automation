"""Independent, read-only postcondition checks for local actions and patches."""
from __future__ import annotations

import hashlib
import io
import json
import re
import zipfile
from dataclasses import dataclass
from pathlib import Path
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

    def review(self, steps: tuple[ActionRequest, ...], *, evidence: dict[int, dict[str, Any]] | None = None) -> list[Check]:
        checks: list[Check] = []
        for index, action in enumerate(steps, 1):
            observed = (evidence or {}).get(index, {})
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
                status = "unknown" if exists else "contradicted"
                detail = "Target exists; content provenance is not verified"
                try:
                    if exists:
                        actual = self.files.file_digest(action.arguments[target_key])["sha256"]
                        expected = observed.get("expected_sha256", "")
                        if action.tool == "write_text_file":
                            expected = hashlib.sha256(str(action.arguments.get("content", "")).encode("utf-8")).hexdigest()
                        elif action.tool == "copy_file" and not expected:
                            expected = self.files.file_digest(action.arguments["source"])["sha256"]
                        if expected:
                            status = "verified" if actual == expected else "contradicted"
                            detail = "Destination digest matches expected bytes" if actual == expected else "Destination digest differs"
                        if action.tool == "move_file" and self.files.resolve(action.arguments["source"], must_exist=False).exists():
                            status, detail = "contradicted", "Move source still exists"
                except (AutomationError, OSError):
                    status, detail = "unknown", "Content could not be independently read/compared"
                checks.append(Check(index, action.tool, status, detail))
            elif action.tool == "archive_files":
                status, detail = self._verify_archive(action.arguments["archive_path"], observed.get("expected_manifest"))
                checks.append(Check(index, action.tool, status, detail))
            elif action.tool in {"workspace_apply_patch", "workspace_revert_patch"} and self.database is not None:
                status, detail = self._verify_patch(str(action.arguments.get("patch_id", "")),
                                                   reverted=action.tool == "workspace_revert_patch")
                checks.append(Check(index, action.tool, status, detail))
            else:
                checks.append(Check(index, action.tool, "unknown", "No independent postcondition check is available"))
        return checks

    def _verify_archive(self, path: str, manifest: Any) -> tuple[str, str]:
        if not isinstance(manifest, dict) or len(manifest) > 200:
            return "unknown", "No trusted bounded archive manifest available"
        try:
            limit = self.files.config.max_read_bytes * 5
            raw = self.files._safe_read_bytes(self.files.resolve(path), limit + 200_000)
            if len(raw) > limit + 200_000:
                return "unknown", "Archive exceeds verification budget"
            with zipfile.ZipFile(io.BytesIO(raw)) as archive:
                entries = archive.infolist()
                names = [entry.filename for entry in entries]
                if len(names) != len(set(names)) or set(names) != set(manifest):
                    return "contradicted", "Archive entries differ from captured source manifest"
                if sum(entry.file_size for entry in entries) > limit:
                    return "contradicted", "Archive expansion exceeds verification limit"
                for entry in entries:
                    if entry.file_size > self.files.config.max_read_bytes or entry.flag_bits & 1:
                        return "unknown", "Archive entry exceeds supported verification limits"
                    with archive.open(entry) as stream:
                        payload = stream.read(self.files.config.max_read_bytes + 1)
                    expected = manifest[entry.filename]
                    if len(payload) != expected["bytes"] or hashlib.sha256(payload).hexdigest() != expected["sha256"]:
                        return "contradicted", "Archive entry bytes differ from captured source"
            return "verified", "Archive entry names, lengths, CRCs and SHA-256 digests match captured source bytes"
        except (AutomationError, OSError, ValueError, KeyError, TypeError, zipfile.BadZipFile, RuntimeError):
            return "unknown", "Archive could not be safely verified"

    def _verify_patch(self, patch_id: str, *, reverted: bool = False) -> tuple[str, str]:
        if self.database is None:
            return "unknown", "Patch database unavailable"
        row = self.database.fetch_one("""SELECT p.*, w.root_path, w.approved FROM coding_patches p
            JOIN coding_workspaces w ON w.workspace_id=p.workspace_id WHERE p.patch_id=?""", (patch_id,))
        if not row or not row["approved"]:
            return "unknown", "No approved workspace/patch contract"
        if row["state"] != ("REVERTED" if reverted else "APPLIED"):
            return "contradicted", "Patch is not in expected terminal state"
        try:
            root = self.files.resolve(row["root_path"])
            entries = json.loads(row["files_json"])
            backup = json.loads(row["backup_json"])
            if not isinstance(entries, list) or not 1 <= len(entries) <= 200:
                return "unknown", "Unsupported patch manifest"
            total = 0
            for entry in entries:
                relative = entry["path"]
                candidate = root / relative
                if Path(relative).is_absolute() or not candidate.resolve().is_relative_to(root):
                    return "unknown", "Patch entry escapes workspace"
                target = self.files.resolve(str(candidate), must_exist=False)
                if reverted and not backup[relative]["existed"]:
                    if target.exists():
                        return "contradicted", "New patch file still exists after revert"
                    continue
                expected = backup[relative]["sha256"] if reverted else entry["new_sha256"]
                raw = self.files._safe_read_bytes(target, self.files.config.max_read_bytes)
                total += len(raw)
                if len(raw) > self.files.config.max_read_bytes or total > 10_000_000:
                    return "unknown", "Patch verification byte budget exceeded"
                # Match WorkspaceManager's universal-newline text contract on Windows.
                text = raw.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")
                if hashlib.sha256(text.encode("utf-8")).hexdigest() != expected:
                    return "contradicted", "Current workspace content differs from patch contract"
            return "verified", "Workspace contents independently match patch before/after digests"
        except (AutomationError, OSError, ValueError, TypeError, KeyError):
            return "unknown", "Patch filesystem evidence unavailable or invalid"

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
                actual = self.files.file_digest(str(p))["sha256"]
                expected = hashlib.sha256(str(arguments.get("content", "")).encode("utf-8")).hexdigest()
                if actual == expected:
                    return "verified", f"File {p} matches expected written bytes"
                return "unknown", f"File {p} exists with differing content"
            except (AutomationError, OSError) as exc:
                return "unknown", str(exc)
        if tool in {"workspace_apply_patch", "workspace_revert_patch"}:
            return self._verify_patch(str(arguments.get("patch_id", "")), reverted=tool == "workspace_revert_patch")
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
