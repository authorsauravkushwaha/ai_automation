"""Windows application discovery and launch control; never invokes a command shell."""

from __future__ import annotations

import difflib
import json
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from hinata.config import Application


class AutomationError(RuntimeError):
    pass


# Protected system processes that close_application must never terminate.
PROTECTED_PROCESSES = frozenset(
    {
        "explorer.exe",
        "svchost.exe",
        "csrss.exe",
        "wininit.exe",
        "winlogon.exe",
        "services.exe",
        "lsass.exe",
        "smss.exe",
        "dwm.exe",
        "system",
    }
)

# Friendly spoken/typed aliases -> candidate lookup names or executable stems
COMMON_APP_ALIASES: dict[str, tuple[str, ...]] = {
    "calc": ("calculator", "calc"),
    "file explorer": ("explorer",),
    "windows explorer": ("explorer",),
    "vs code": ("visual studio code", "code"),
    "vscode": ("visual studio code", "code"),
    "code": ("visual studio code", "code"),
    "chrome": ("google chrome", "chrome"),
    "google chrome": ("google chrome", "chrome"),
    "edge": ("microsoft edge", "msedge"),
    "ms edge": ("microsoft edge", "msedge"),
    "browser": ("microsoft edge", "google chrome", "firefox", "brave"),
    "firefox": ("mozilla firefox", "firefox"),
    "brave": ("brave", "brave browser"),
    "word": ("word", "microsoft word", "winword"),
    "ms word": ("word", "microsoft word", "winword"),
    "excel": ("excel", "microsoft excel"),
    "ms excel": ("excel", "microsoft excel"),
    "powerpoint": ("powerpoint", "microsoft powerpoint", "powerpnt"),
    "ppt": ("powerpoint", "microsoft powerpoint", "powerpnt"),
    "outlook": ("outlook", "microsoft outlook"),
    "onenote": ("onenote",),
    "teams": ("microsoft teams", "ms-teams", "teams"),
    "paint": ("paint", "mspaint"),
    "ms paint": ("paint", "mspaint"),
    "cmd": ("command prompt", "cmd"),
    "command prompt": ("command prompt", "cmd"),
    "terminal": ("windows terminal", "wt", "powershell"),
    "windows terminal": ("windows terminal", "wt"),
    "powershell": ("windows powershell", "powershell"),
    "task manager": ("task manager", "taskmgr"),
    "control panel": ("control panel", "control"),
    "settings": ("settings", "windows settings"),
    "windows settings": ("settings", "windows settings"),
    "snipping tool": ("snipping tool", "snippingtool"),
    "vlc": ("vlc media player", "vlc"),
    "obs": ("obs studio", "obs64"),
    "git bash": ("git bash",),
    "store": ("microsoft store",),
}

# Built-in Windows 11 executables/URIs available on standard Windows installations
BUILTIN_WINDOWS_APPS: dict[str, tuple[str, str, list[str]]] = {
    # key -> (launch_kind, target, process_names)
    "paint": ("exe", "mspaint.exe", ["mspaint.exe"]),
    "command prompt": ("exe", "cmd.exe", ["cmd.exe"]),
    "powershell": ("exe", "powershell.exe", ["powershell.exe"]),
    "windows terminal": ("exe", "wt.exe", ["WindowsTerminal.exe", "wt.exe"]),
    "task manager": ("exe", "taskmgr.exe", ["Taskmgr.exe"]),
    "control panel": ("exe", "control.exe", []),
    "snipping tool": ("exe", "snippingtool.exe", ["SnippingTool.exe"]),
    "character map": ("exe", "charmap.exe", ["charmap.exe"]),
    "settings": ("uri", "ms-settings:", ["SystemSettings.exe"]),
}

_SKIP_SHORTCUT_WORDS = ("uninstall", "remove ", "readme", "release notes", "documentation", "help ", "website")


def _normalize_app_key(raw: str) -> str:
    cleaned = re.sub(r"\.exe$|\.lnk$|\.appref-ms$", "", raw.strip().lower())
    cleaned = re.sub(r"[^a-z0-9+#.\- ]+", " ", cleaned)
    return " ".join(cleaned.split())


@dataclass(slots=True)
class DiscoveredApp:
    key: str
    display_name: str
    launch_kind: str  # "command", "shortcut", "uwp", "uri"
    target: str
    command: list[str]
    process_names: list[str]
    source: str  # "configured", "start_menu", "registry", "uwp", "builtin"


class AppControl:
    """Discovers and launches configured or user-installed Windows applications safely."""

    CACHE_TTL_SECONDS = 120.0

    def __init__(
        self,
        applications: dict[str, Application],
        *,
        discover_installed: bool = True,
        extra_shortcut_dirs: list[Path] | None = None,
    ) -> None:
        self.applications = applications
        self.discover_installed = discover_installed
        self._extra_shortcut_dirs = list(extra_shortcut_dirs or [])
        self._catalog_cache: dict[str, DiscoveredApp] = {}
        self._cache_timestamp: float = 0.0
        self._uwp_loaded = False

    def _base_configured_catalog(self) -> dict[str, DiscoveredApp]:
        catalog: dict[str, DiscoveredApp] = {}
        for name, app in self.applications.items():
            norm = _normalize_app_key(name)
            if norm:
                catalog[norm] = DiscoveredApp(
                    key=norm,
                    display_name=name,
                    launch_kind="command",
                    target=app.command[0],
                    command=list(app.command),
                    process_names=list(app.process_names),
                    source="configured",
                )
        return catalog

    def _shortcut_directories(self) -> list[Path]:
        dirs = list(self._extra_shortcut_dirs)
        if os.name == "nt":
            for env_key, rel in (
                ("APPDATA", r"Microsoft\Windows\Start Menu\Programs"),
                ("PROGRAMDATA", r"Microsoft\Windows\Start Menu\Programs"),
                ("USERPROFILE", "Desktop"),
                ("PUBLIC", "Desktop"),
            ):
                base = os.environ.get(env_key)
                if base:
                    candidate = Path(base) / rel
                    if candidate.is_dir():
                        dirs.append(candidate)
        return dirs

    def _scan_shortcuts(self, catalog: dict[str, DiscoveredApp]) -> None:
        for root_dir in self._shortcut_directories():
            if not root_dir.is_dir():
                continue
            visited_dirs = 0
            for current, subdirs, filenames in os.walk(root_dir):
                visited_dirs += 1
                if visited_dirs > 250:
                    break
                subdirs[:] = [d for d in subdirs if not d.startswith(".")]
                for fname in filenames:
                    lower = fname.lower()
                    if not lower.endswith((".lnk", ".appref-ms")):
                        continue
                    stem = Path(fname).stem
                    norm = _normalize_app_key(stem)
                    if not norm or any(skip in norm for skip in _SKIP_SHORTCUT_WORDS):
                        continue
                    if norm in catalog:
                        continue
                    full_path = str(Path(current) / fname)
                    catalog[norm] = DiscoveredApp(
                        key=norm,
                        display_name=stem,
                        launch_kind="shortcut",
                        target=full_path,
                        command=[],
                        process_names=[],
                        source="start_menu",
                    )

    def _scan_windows_registry(self, catalog: dict[str, DiscoveredApp]) -> None:
        if os.name != "nt":  # pragma: no cover - Windows registry discovery
            return
        try:
            import winreg
        except ImportError:
            return

        roots = [
            (winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\App Paths"),
            (winreg.HKEY_LOCAL_MACHINE, r"Software\Microsoft\Windows\CurrentVersion\App Paths"),
        ]
        for hive, subkey in roots:
            try:
                with winreg.OpenKey(hive, subkey) as key_handle:
                    idx = 0
                    while idx < 400:
                        try:
                            exe_name = winreg.EnumKey(key_handle, idx)
                        except OSError:
                            break
                        idx += 1
                        norm = _normalize_app_key(exe_name)
                        if not norm or norm in catalog:
                            continue
                        try:
                            with winreg.OpenKey(key_handle, exe_name) as app_key:
                                exe_path, _ = winreg.QueryValueEx(app_key, "")
                        except OSError:
                            continue
                        cleaned_path = str(exe_path).strip().strip('"')
                        if cleaned_path and Path(cleaned_path).is_file():
                            proc_name = Path(cleaned_path).name
                            catalog[norm] = DiscoveredApp(
                                key=norm,
                                display_name=Path(exe_name).stem,
                                launch_kind="command",
                                target=cleaned_path,
                                command=[cleaned_path],
                                process_names=[proc_name] if proc_name.lower() not in PROTECTED_PROCESSES else [],
                                source="registry",
                            )
            except OSError:
                continue

        # Scan Uninstall keys for DisplayName -> DisplayIcon .exe
        uninstall_paths = [
            (winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Uninstall"),
            (winreg.HKEY_LOCAL_MACHINE, r"Software\Microsoft\Windows\CurrentVersion\Uninstall"),
            (winreg.HKEY_LOCAL_MACHINE, r"Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
        ]
        for hive, subkey in uninstall_paths:
            try:
                with winreg.OpenKey(hive, subkey) as u_handle:
                    idx = 0
                    while idx < 500:
                        try:
                            child_name = winreg.EnumKey(u_handle, idx)
                        except OSError:
                            break
                        idx += 1
                        try:
                            with winreg.OpenKey(u_handle, child_name) as item_key:
                                disp_name, _ = winreg.QueryValueEx(item_key, "DisplayName")
                                icon_raw, _ = winreg.QueryValueEx(item_key, "DisplayIcon")
                        except OSError:
                            continue
                        norm = _normalize_app_key(str(disp_name))
                        if not norm or norm in catalog or any(s in norm for s in _SKIP_SHORTCUT_WORDS):
                            continue
                        exe_candidate = str(icon_raw).split(",", 1)[0].strip().strip('"')
                        if exe_candidate.lower().endswith(".exe") and "unins" not in exe_candidate.lower() and Path(exe_candidate).is_file():
                            proc_name = Path(exe_candidate).name
                            catalog[norm] = DiscoveredApp(
                                key=norm,
                                display_name=str(disp_name).strip(),
                                launch_kind="command",
                                target=exe_candidate,
                                command=[exe_candidate],
                                process_names=[proc_name] if proc_name.lower() not in PROTECTED_PROCESSES else [],
                                source="registry",
                            )
            except OSError:
                continue

    def _scan_builtins(self, catalog: dict[str, DiscoveredApp]) -> None:
        for key, (kind, target, procs) in BUILTIN_WINDOWS_APPS.items():
            if key in catalog:
                continue
            if kind == "uri":
                catalog[key] = DiscoveredApp(
                    key=key,
                    display_name=key.title(),
                    launch_kind="uri",
                    target=target,
                    command=[],
                    process_names=list(procs),
                    source="builtin",
                )
            else:
                resolved = shutil.which(target) or target
                catalog[key] = DiscoveredApp(
                    key=key,
                    display_name=key.title(),
                    launch_kind="command",
                    target=resolved,
                    command=[resolved],
                    process_names=list(procs),
                    source="builtin",
                )

    def _scan_uwp_start_apps(self, catalog: dict[str, DiscoveredApp]) -> None:
        """Load packaged Microsoft Store / Start apps (e.g. WhatsApp, Store, Photos, Spotify)."""
        if os.name != "nt" or self._uwp_loaded:  # pragma: no cover - Windows StartApps
            return
        self._uwp_loaded = True
        try:
            proc = subprocess.run(
                [
                    "powershell.exe",
                    "-NoProfile",
                    "-NonInteractive",
                    "-Command",
                    "Get-StartApps | Select-Object Name, AppID | ConvertTo-Json -Compress",
                ],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
                shell=False,
            )
            if proc.returncode != 0 or not proc.stdout.strip():
                return
            parsed = json.loads(proc.stdout)
            entries = parsed if isinstance(parsed, list) else ([parsed] if isinstance(parsed, dict) else [])
            for entry in entries[:400]:
                if not isinstance(entry, dict):
                    continue
                name = str(entry.get("Name") or "").strip()
                app_id = str(entry.get("AppID") or "").strip()
                norm = _normalize_app_key(name)
                if (
                    not norm
                    or not app_id
                    or norm in catalog
                    or any(c in app_id for c in ("\x00", "\r", "\n", '"'))
                    or any(s in norm for s in _SKIP_SHORTCUT_WORDS)
                ):
                    continue
                catalog[norm] = DiscoveredApp(
                    key=norm,
                    display_name=name,
                    launch_kind="uwp",
                    target=app_id,
                    command=["explorer.exe", f"shell:AppsFolder\\{app_id}"],
                    process_names=[],
                    source="uwp",
                )
        except Exception:
            return

    def _catalog(self, *, refresh: bool = False, include_uwp: bool = False) -> dict[str, DiscoveredApp]:
        if not self.discover_installed:
            return self._base_configured_catalog()
        now = time.monotonic()
        if refresh or not self._catalog_cache or (now - self._cache_timestamp > self.CACHE_TTL_SECONDS):
            catalog = self._base_configured_catalog()
            self._scan_shortcuts(catalog)
            self._scan_windows_registry(catalog)
            self._scan_builtins(catalog)
            self._catalog_cache = catalog
            self._cache_timestamp = now
            self._uwp_loaded = False
        if include_uwp and not self._uwp_loaded:
            self._scan_uwp_start_apps(self._catalog_cache)
        return self._catalog_cache

    def _match_in_catalog(self, query_key: str, catalog: dict[str, DiscoveredApp]) -> DiscoveredApp | None:
        if query_key in catalog:
            return catalog[query_key]

        # Check friendly aliases first
        for candidate_alias in COMMON_APP_ALIASES.get(query_key, ()):
            norm_alias = _normalize_app_key(candidate_alias)
            if norm_alias in catalog:
                return catalog[norm_alias]

        # Word-boundary / prefix match (e.g. "chrome" -> "google chrome", "telegram" -> "telegram desktop")
        prefix_hits = [
            app for key, app in catalog.items()
            if key.startswith(query_key + " ") or key.endswith(" " + query_key) or query_key in key.split()
        ]
        if prefix_hits:
            prefix_hits.sort(key=lambda a: (0 if a.source == "configured" else 1, len(a.key)))
            return prefix_hits[0]

        # Substring match for longer names (>= 4 chars)
        if len(query_key) >= 4:
            sub_hits = [app for key, app in catalog.items() if query_key in key]
            if sub_hits:
                sub_hits.sort(key=lambda a: len(a.key))
                return sub_hits[0]

        # Close fuzzy typo match (e.g. "notpad" -> "notepad", "spotfy" -> "spotify")
        close = difflib.get_close_matches(query_key, list(catalog.keys()), n=1, cutoff=0.78)
        if close:
            return catalog[close[0]]
        return None

    def resolve_application(self, name: str) -> DiscoveredApp:
        query_key = _normalize_app_key(name)
        if not query_key:
            raise AutomationError("Application name must not be empty")

        catalog = self._catalog()
        matched = self._match_in_catalog(query_key, catalog)
        if matched is None and self.discover_installed:
            # Refresh including UWP Start Menu apps before giving up
            catalog = self._catalog(refresh=True, include_uwp=True)
            matched = self._match_in_catalog(query_key, catalog)
        if matched is None:
            sample = ", ".join(sorted(catalog)[:12])
            suffix = " ..." if len(catalog) > 12 else ""
            if not self.discover_installed:
                allowed = ", ".join(sorted(self.applications))
                raise AutomationError(f"Application '{name}' is not allow-listed. Available: {allowed}")
            raise AutomationError(
                f"Application '{name}' was not found among installed apps. Available examples: {sample}{suffix}"
            )
        return matched

    def discover_installed_apps(self, *, refresh: bool = False, include_uwp: bool = True) -> list[dict[str, Any]]:
        """Public cached catalog refresh/discovery API."""
        catalog = self._catalog(refresh=refresh, include_uwp=include_uwp)
        return [
            {"name": app.display_name, "key": app.key, "source": app.source, "launch_kind": app.launch_kind}
            for _, app in sorted(catalog.items())
        ]

    def list_installed_apps(self, query: str = "", limit: int = 60) -> dict[str, Any]:
        return self.list_installed(query, limit)

    def list_installed(self, query: str = "", limit: int = 60) -> dict[str, Any]:
        """Return discovered user & system applications filtered by optional query."""
        catalog = self._catalog(include_uwp=True)
        needle = _normalize_app_key(query)
        items = [
            {
                "name": app.display_name,
                "key": app.key,
                "source": app.source,
                "launch_kind": app.launch_kind,
            }
            for _, app in sorted(catalog.items())
            if not needle or needle in app.key or needle in app.display_name.lower()
        ]
        cap = max(1, min(int(limit), 200))
        return {
            "count": len(items),
            "showing": min(len(items), cap),
            "applications": items[:cap],
        }

    def open(self, name: str) -> dict[str, Any]:
        app = self.resolve_application(name)
        if os.name != "nt":
            raise AutomationError("Application control is available only on Windows 11")
        try:
            if app.launch_kind in {"shortcut", "uri"}:
                startfile = getattr(os, "startfile", None)
                if not callable(startfile):
                    raise AutomationError("Windows shell launcher os.startfile is unavailable")
                startfile(app.target)
                return {
                    "application": app.display_name,
                    "key": app.key,
                    "source": app.source,
                    "pid": None,
                }
            process = subprocess.Popen(
                app.command,
                shell=False,
                close_fds=True,
                creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise AutomationError(f"Could not open {app.display_name}: {exc}") from exc
        return {
            "application": app.display_name,
            "key": app.key,
            "source": app.source,
            "pid": process.pid,
        }

    def close(self, name: str) -> dict[str, Any]:
        app = self.resolve_application(name)
        if os.name != "nt":
            raise AutomationError("Application control is available only on Windows 11")
        process_names = [p for p in app.process_names if p.lower() not in PROTECTED_PROCESSES]
        if not process_names:
            raise AutomationError(f"Closing {app.key} is disabled by its safety configuration")
        closed: list[str] = []
        errors: list[str] = []
        for process_name in process_names:
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
            raise AutomationError(f"Could not close {app.key}: {'; '.join(errors)}")
        return {
            "application": app.key,
            "closed_processes": closed,
            "warnings": errors,
            "already_closed": not closed and not errors,
        }
