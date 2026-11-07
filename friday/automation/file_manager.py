"""Bounded file operations restricted to configured roots."""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

from friday.automation.app_control import AutomationError
from friday.config import SecurityConfig

BLOCKED_OPEN_SUFFIXES = {
    ".exe", ".com", ".bat", ".cmd", ".ps1", ".msi", ".msp", ".scr",
    ".vbs", ".vbe", ".js", ".jse", ".wsf", ".wsh", ".jar", ".lnk",
}


class FileManager:
    def __init__(self, config: SecurityConfig) -> None:
        self.config = config

    def _inside_root(self, path: Path, root: Path) -> bool:
        try:
            path.relative_to(root)
            return True
        except ValueError:
            return False

    def resolve(self, value: str, *, must_exist: bool = True) -> Path:
        if not value.strip():
            raise AutomationError("A file or folder path is required")
        supplied = Path(value.strip().strip('"')).expanduser()
        candidates = [supplied] if supplied.is_absolute() else [root / supplied for root in self.config.allowed_roots]
        if not supplied.is_absolute():
            for root in self.config.allowed_roots:
                parts = supplied.parts
                if parts and parts[0].casefold() == root.name.casefold():
                    # Accept both "note.txt" and "Documents/note.txt" for the Documents root.
                    candidates.insert(0, root.joinpath(*parts[1:]))
        fallback = candidates[0]
        for candidate in candidates:
            try:
                resolved = candidate.resolve(strict=False)
            except OSError as exc:
                raise AutomationError(f"Cannot resolve path: {exc}") from exc
            if not any(self._inside_root(resolved, root) for root in self.config.allowed_roots):
                continue
            if not must_exist or resolved.exists():
                return resolved
        resolved_fallback = fallback.resolve(strict=False)
        if not any(self._inside_root(resolved_fallback, root) for root in self.config.allowed_roots):
            raise AutomationError("Access denied: the path is outside all permitted folders")
        if must_exist:
            raise AutomationError(f"Path does not exist inside permitted folders: {value}")
        return resolved_fallback

    def open_folder(self, value: str) -> dict[str, Any]:
        path = self.resolve(value)
        if not path.is_dir():
            raise AutomationError(f"Not a folder: {path}")
        self._open_with_windows(path)
        return {"path": str(path)}

    def open_file(self, value: str) -> dict[str, Any]:
        path = self.resolve(value)
        if not path.is_file():
            raise AutomationError(f"Not a file: {path}")
        if path.suffix.lower() in BLOCKED_OPEN_SUFFIXES:
            raise AutomationError("Executable, shortcut, and script files cannot be opened by FRIDAY")
        self._open_with_windows(path)
        return {"path": str(path)}

    @staticmethod
    def _open_with_windows(path: Path) -> None:
        if os.name != "nt":
            raise AutomationError("Opening files with desktop applications is supported only on Windows 11")
        try:
            os.startfile(str(path))  # type: ignore[attr-defined]
        except OSError as exc:
            raise AutomationError(f"Windows could not open {path}: {exc}") from exc

    def create_folder(self, value: str) -> dict[str, Any]:
        path = self.resolve(value, must_exist=False)
        try:
            path.mkdir(parents=True, exist_ok=False)
        except FileExistsError as exc:
            raise AutomationError(f"Folder already exists: {path}") from exc
        except OSError as exc:
            raise AutomationError(f"Could not create folder: {exc}") from exc
        return {"path": str(path)}

    def read_text(self, value: str, max_chars: int = 12000) -> dict[str, Any]:
        path = self.resolve(value)
        if not path.is_file():
            raise AutomationError(f"Not a file: {path}")
        if path.suffix.lower() not in self.config.text_extensions:
            raise AutomationError(f"Files with extension '{path.suffix}' are not approved for text reading")
        try:
            with path.open("rb") as handle:
                raw = handle.read(self.config.max_read_bytes + 1)
        except OSError as exc:
            raise AutomationError(f"Could not read {path}: {exc}") from exc
        truncated = len(raw) > self.config.max_read_bytes
        decoded = raw[: self.config.max_read_bytes].decode("utf-8-sig", errors="replace")
        truncated = truncated or len(decoded) > max_chars
        return {"path": str(path), "content": decoded[:max_chars], "truncated": truncated}

    def search(self, query: str, root: str = "", max_results: int = 20) -> dict[str, Any]:
        roots = [self.resolve(root)] if root.strip() else list(self.config.allowed_roots)
        deadline = time.monotonic() + self.config.search_timeout_seconds
        needle = query.casefold()
        matches: list[str] = []
        visited = 0
        timed_out = False
        for base in roots:
            if not base.exists() or not base.is_dir():
                continue
            for directory, dirnames, filenames in os.walk(base, followlinks=False):
                visited += 1
                if visited > self.config.search_max_directories or time.monotonic() >= deadline:
                    timed_out = True
                    break
                dirnames[:] = sorted(
                    name for name in dirnames if not name.startswith(".") and name not in {"$RECYCLE.BIN", "System Volume Information"}
                )
                for filename in sorted(filenames):
                    if needle in filename.casefold():
                        candidate = (Path(directory) / filename).resolve(strict=False)
                        if any(self._inside_root(candidate, allowed) for allowed in self.config.allowed_roots):
                            matches.append(str(candidate))
                            if len(matches) >= max_results:
                                return {"matches": matches, "truncated": True, "directories_searched": visited}
                if time.monotonic() >= deadline:
                    timed_out = True
                    break
            if timed_out or len(matches) >= max_results:
                break
        return {"matches": matches, "truncated": timed_out, "directories_searched": visited}
