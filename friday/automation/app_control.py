"""Allow-listed Windows application control; never invokes a shell."""

from __future__ import annotations

import os
import subprocess
from typing import Any

from friday.config import Application


class AutomationError(RuntimeError):
    pass


class AppControl:
    def __init__(self, applications: dict[str, Application]) -> None:
        self.applications = applications

    def _application(self, name: str) -> tuple[str, Application]:
        key = name.strip().lower()
        if key not in self.applications:
            allowed = ", ".join(sorted(self.applications))
            raise AutomationError(f"Application '{name}' is not allow-listed. Available: {allowed}")
        return key, self.applications[key]

    def open(self, name: str) -> dict[str, Any]:
        key, app = self._application(name)
        if os.name != "nt":
            raise AutomationError("Application control is available only on Windows 11")
        try:
            process = subprocess.Popen(
                app.command,
                shell=False,
                close_fds=True,
                creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise AutomationError(f"Could not open {key}: {exc}") from exc
        return {"application": key, "pid": process.pid}

    def close(self, name: str) -> dict[str, Any]:
        key, app = self._application(name)
        if os.name != "nt":
            raise AutomationError("Application control is available only on Windows 11")
        if not app.process_names:
            raise AutomationError(f"Closing {key} is disabled by its safety configuration")
        closed: list[str] = []
        errors: list[str] = []
        for process_name in app.process_names:
            try:
                result = subprocess.run(
                    ["taskkill.exe", "/IM", process_name, "/T"],
                    capture_output=True,
                    text=True,
                    timeout=15,
                    check=False,
                    shell=False,
                )
            except (OSError, subprocess.SubprocessError) as exc:
                errors.append(str(exc))
                continue
            output = (result.stdout or result.stderr).strip()
            if result.returncode == 0:
                closed.append(process_name)
            elif "not found" not in output.lower() and "not running" not in output.lower():
                errors.append(output or f"taskkill exited with {result.returncode}")
        if errors and not closed:
            raise AutomationError(f"Could not close {key}: {'; '.join(errors)}")
        return {
            "application": key,
            "closed_processes": closed,
            "warnings": errors,
            "already_closed": not closed and not errors,
        }
