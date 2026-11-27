"""J.A.R.V.I.S.-style Command & Desktop Cockpit Engine — deep local machine control under strict owner authority.

Capabilities (cross-platform Linux/Windows where OS APIs exist, zero mandatory 3rd-party deps):
1. ``list_windows``: Enumerate visible desktop windows (Win32 ``EnumWindows`` on Windows; ``wmctrl`` / ``xdotool`` on Linux).
2. ``focus_window`` / ``snap_window``: Bring a matching window to foreground or arrange it (``left``, ``right``, ``maximize``, ``minimize``, ``restore``).
3. ``send_hotkey``: Send strictly allow-listed productivity shortcuts (``copy``, ``paste``, ``save``, ``undo``, ``redo``, ``find``, ``new_tab``, ``close_tab``, ``switch_tab``, ``select_all``) with dangerous OS-kill combos (``alt+f4``, ``ctrl+alt+del``, ``win+r``) permanently blocked.
4. ``read_clipboard`` / ``write_clipboard``: Read (with automatic secret redaction preview) or set local clipboard text.
5. ``capture_screen``: Capture primary screen to an owner-approved workspace path and optionally pass through ``LocalVisionAdapter`` offline OCR/vision.
6. ``list_processes``: Live CPU/RAM process table with top memory consumers (``/proc`` on Linux, ``tasklist /FO CSV`` on Windows).
7. ``power_control``: Lock workstation immediately (``CONFIRM``), or initiate OS sleep/hibernate (``SENSITIVE`` PIN-gated).
8. ``executive_briefing``: J.A.R.V.I.S. tactical situational awareness report combining hardware vitals, battery/power, active tasks, pending approvals, scheduled jobs, security posture, and audit integrity.
9. ``JarvisOverrideController``: Owner Tactical Protocols — **Red Alert Lockdown** (instant kill-switch freezing all autonomous & write/UI tools until PIN unlock) and **PIN-unlocked Timed Commander Lease** (temporary 1–30 min owner authority session with instant ``stand down`` revocation).
"""

from __future__ import annotations

import csv
import io
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

from hinata.automation.app_control import AutomationError
from hinata.automation.file_manager import FileManager
from hinata.security.redaction import contains_secrets, redact_text

# Strictly allow-listed productivity hotkeys mapped to Win32 Virtual-Key codes & xdotool combos.
SAFE_HOTKEYS: dict[str, tuple[tuple[int, ...], str]] = {
    "copy": ((0x11, 0x43), "ctrl+c"),
    "paste": ((0x11, 0x56), "ctrl+v"),
    "cut": ((0x11, 0x58), "ctrl+x"),
    "save": ((0x11, 0x53), "ctrl+s"),
    "undo": ((0x11, 0x5A), "ctrl+z"),
    "redo": ((0x11, 0x59), "ctrl+y"),
    "select_all": ((0x11, 0x41), "ctrl+a"),
    "find": ((0x11, 0x46), "ctrl+f"),
    "new_tab": ((0x11, 0x54), "ctrl+t"),
    "close_tab": ((0x11, 0x57), "ctrl+w"),
    "switch_tab": ((0x11, 0x09), "ctrl+Tab"),
    "enter": ((0x0D,), "Return"),
    "escape": ((0x1B,), "Escape"),
}

WINDOW_LAYOUTS = frozenset({"maximize", "minimize", "restore", "left", "right"})
POWER_ACTIONS = frozenset({"lock", "sleep", "hibernate"})


class JarvisOverrideController:
    """Owner-governed tactical protocols: Red Alert Lockdown & PIN-unlocked Commander Lease.

    Safety invariants:
    - Red Alert Lockdown can be engaged INSTANTLY without a PIN (panic button / 'lockdown protocol'),
      blocking every non-status/mutating tool immediately.
    - Disengaging Red Alert Lockdown or activating a Commander Lease ALWAYS requires a verified owner PIN.
    - Even under an active Commander Lease, ``SENSITIVE`` destructive tools (e.g., ``clear_memory``,
      ``quarantine_delete_file``) are NEVER silently auto-executed from untrusted model output unless
      explicitly authorized. The lease streamlines ``CONFIRM``-tier interactive friction for a bounded
      TTL (1–30 minutes) and can be revoked in 0 ms via ``stand_down()``.
    """

    LOCKDOWN_EXEMPT_TOOLS = frozenset(
        {"system_info", "hardware_profile", "ai_fleet_profile", "executive_briefing", "list_preferences"}
    )

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._lockdown_active = False
        self._lockdown_reason = ""
        self._lockdown_since: float = 0.0
        self._commander_until: float = 0.0
        self._commander_label: str = ""

    def engage_lockdown(self, reason: str = "Owner engaged Red Alert Lockdown") -> dict[str, Any]:
        with self._lock:
            self._lockdown_active = True
            self._lockdown_reason = reason[:200]
            self._lockdown_since = time.time()
            # Engaging lockdown immediately revokes any active commander lease.
            self._commander_until = 0.0
            self._commander_label = ""
            return self.status()

    def release_lockdown(self, *, pin_verified: bool) -> dict[str, Any]:
        if not pin_verified:
            raise PermissionError("Owner PIN verification is required to lift Red Alert Lockdown")
        with self._lock:
            self._lockdown_active = False
            self._lockdown_reason = ""
            self._lockdown_since = 0.0
            return self.status()

    def activate_commander_lease(
        self,
        *,
        pin_verified: bool,
        minutes: int = 10,
        label: str = "Owner Commander Session",
    ) -> dict[str, Any]:
        if not pin_verified:
            raise PermissionError("Owner PIN verification is required to activate Commander Mode")
        bounded_min = max(1, min(int(minutes), 30))
        with self._lock:
            if self._lockdown_active:
                raise PermissionError("Cannot activate Commander Mode while Red Alert Lockdown is active")
            self._commander_until = time.time() + bounded_min * 60.0
            self._commander_label = label[:120]
            return self.status()

    def stand_down(self) -> dict[str, Any]:
        """Immediately revoke any active Commander Mode lease without needing a PIN."""
        with self._lock:
            self._commander_until = 0.0
            self._commander_label = ""
            return self.status()

    @property
    def is_lockdown(self) -> bool:
        with self._lock:
            return self._lockdown_active

    @property
    def is_commander_active(self) -> bool:
        with self._lock:
            if self._lockdown_active:
                return False
            if self._commander_until > time.time():
                return True
            self._commander_until = 0.0
            return False

    def is_tool_blocked_by_lockdown(self, tool: str) -> bool:
        with self._lock:
            if not self._lockdown_active:
                return False
            return tool not in self.LOCKDOWN_EXEMPT_TOOLS

    def status(self) -> dict[str, Any]:
        now = time.time()
        commander_rem = max(0.0, self._commander_until - now) if not self._lockdown_active else 0.0
        return {
            "lockdown_active": self._lockdown_active,
            "lockdown_reason": self._lockdown_reason,
            "lockdown_since_epoch": round(self._lockdown_since, 3) if self._lockdown_since else None,
            "commander_mode_active": commander_rem > 0.0,
            "commander_remaining_seconds": round(commander_rem, 1),
            "commander_label": self._commander_label if commander_rem > 0.0 else "",
        }


class DesktopCockpit:
    """Cross-platform window, hotkey, clipboard, screen, process, and power cockpit."""

    def __init__(self, files: FileManager | None = None, *, user32: Any | None = None) -> None:
        self.files = files
        self._user32 = user32

    def _win_user32(self) -> Any:
        if self._user32 is not None:
            return self._user32
        import ctypes

        windll = getattr(ctypes, "windll", None)
        if windll is None:
            raise AutomationError("Win32 user32 API is only available on Windows")
        return windll.user32

    def list_windows(self, *, filter_text: str = "", limit: int = 40) -> dict[str, Any]:
        needle = filter_text.strip().casefold()
        cap = max(1, min(limit, 100))
        windows: list[dict[str, Any]] = []

        if os.name == "nt" or self._user32 is not None:
            import ctypes

            u32 = self._win_user32()
            fg_hwnd = int(u32.GetForegroundWindow() or 0)
            win_fn_factory = getattr(ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE)

            @win_fn_factory(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
            def _enum_cb(hwnd: int, _lparam: int) -> bool:
                if not hwnd or not u32.IsWindowVisible(hwnd):
                    return True
                length = int(u32.GetWindowTextLengthW(hwnd) or 0)
                if length <= 0:
                    return True
                buf = ctypes.create_unicode_buffer(length + 1)
                u32.GetWindowTextW(hwnd, buf, length + 1)
                title = str(buf.value or "").strip()
                if not title:
                    return True
                if needle and needle not in title.casefold():
                    return True
                pid = ctypes.c_ulong(0)
                u32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                windows.append(
                    {
                        "hwnd": int(hwnd),
                        "title": title[:200],
                        "pid": int(pid.value),
                        "active": int(hwnd) == fg_hwnd,
                    }
                )
                return len(windows) < cap

            u32.EnumWindows(_enum_cb, 0)
            return {"platform": "windows", "count": len(windows), "windows": windows}

        # Linux / X11 fallback via wmctrl if installed
        wmctrl = shutil.which("wmctrl")
        if wmctrl:
            res = subprocess.run(
                [wmctrl, "-lp"], capture_output=True, text=True, timeout=3.0, check=False, shell=False
            )
            for line in res.stdout.splitlines():
                parts = line.split(None, 4)
                if len(parts) < 5:
                    continue
                wid, _desk, pid_s, _host, title = parts
                if needle and needle not in title.casefold():
                    continue
                windows.append(
                    {
                        "hwnd": wid,
                        "title": title[:200],
                        "pid": int(pid_s) if pid_s.isdigit() else 0,
                        "active": False,
                    }
                )
                if len(windows) >= cap:
                    break
            return {"platform": "linux_wmctrl", "count": len(windows), "windows": windows}

        return {
            "platform": sys.platform,
            "count": 0,
            "windows": [],
            "note": "Desktop window enumeration requires Windows 11 or Linux X11 'wmctrl'.",
        }

    def focus_window(self, title_query: str) -> dict[str, Any]:
        query = title_query.strip()
        if not query:
            raise AutomationError("Window title query cannot be empty")
        listing = self.list_windows(filter_text=query, limit=10)
        matches = listing["windows"]
        if not matches:
            raise AutomationError(f"No visible window matched '{query}'")
        target = matches[0]

        if os.name == "nt" or self._user32 is not None:
            u32 = self._win_user32()
            hwnd = int(target["hwnd"])
            # SW_RESTORE = 9 if minimized
            if u32.IsIconic(hwnd):
                u32.ShowWindow(hwnd, 9)
            u32.SetForegroundWindow(hwnd)
            return {"focused": True, "window": target}

        wmctrl = shutil.which("wmctrl")
        if wmctrl:
            subprocess.run(
                [wmctrl, "-ia", str(target["hwnd"])],
                capture_output=True,
                timeout=3.0,
                check=False,
                shell=False,
            )
            return {"focused": True, "window": target}
        raise AutomationError("Window focus requires Windows 11 or Linux 'wmctrl'")

    def snap_window(self, title_query: str, layout: str) -> dict[str, Any]:
        clean_layout = layout.strip().lower()
        if clean_layout not in WINDOW_LAYOUTS:
            raise AutomationError(f"Unsupported window layout '{layout}'; choose from {sorted(WINDOW_LAYOUTS)}")
        focused = self.focus_window(title_query)
        target = focused["window"]

        if os.name == "nt" or self._user32 is not None:
            u32 = self._win_user32()
            hwnd = int(target["hwnd"])
            if clean_layout == "maximize":
                u32.ShowWindow(hwnd, 3)  # SW_MAXIMIZE
            elif clean_layout == "minimize":
                u32.ShowWindow(hwnd, 6)  # SW_MINIMIZE
            elif clean_layout == "restore":
                u32.ShowWindow(hwnd, 9)  # SW_RESTORE
            else:
                screen_w = int(u32.GetSystemMetrics(0) or 1920)
                screen_h = int(u32.GetSystemMetrics(1) or 1080)
                u32.ShowWindow(hwnd, 9)
                half_w = screen_w // 2
                x_pos = 0 if clean_layout == "left" else half_w
                u32.MoveWindow(hwnd, x_pos, 0, half_w, screen_h, True)
            return {"window": target, "layout": clean_layout, "applied": True}

        raise AutomationError("Window snapping requires Windows 11")

    def send_hotkey(self, shortcut: str, *, target_window: str = "") -> dict[str, Any]:
        key_name = shortcut.strip().lower()
        if key_name not in SAFE_HOTKEYS:
            raise AutomationError(
                f"Shortcut '{shortcut}' is not in the safe allow-list ({', '.join(sorted(SAFE_HOTKEYS))})"
            )
        if target_window.strip():
            self.focus_window(target_window)
            time.sleep(0.08)

        vk_codes, xdo_combo = SAFE_HOTKEYS[key_name]
        if os.name == "nt" or self._user32 is not None:
            u32 = self._win_user32()
            for vk in vk_codes:
                u32.keybd_event(vk, 0, 0, 0)
            for vk in reversed(vk_codes):
                u32.keybd_event(vk, 0, 2, 0)  # KEYEVENTF_KEYUP = 2
            return {"shortcut": key_name, "sent": True, "target_window": target_window or "active"}

        xdotool = shutil.which("xdotool")
        if xdotool:
            res = subprocess.run(
                [xdotool, "key", "--clearmodifiers", xdo_combo],
                capture_output=True,
                timeout=3.0,
                check=False,
                shell=False,
            )
            if res.returncode == 0:
                return {"shortcut": key_name, "sent": True, "target_window": target_window or "active"}
        raise AutomationError("Sending hotkeys requires Windows 11 or Linux 'xdotool'")

    def read_clipboard(self, *, max_chars: int = 4000) -> dict[str, Any]:
        text = ""
        if os.name == "nt" and self._user32 is None:
            res = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", "Get-Clipboard -Raw"],
                capture_output=True,
                text=True,
                timeout=4.0,
                check=False,
                shell=False,
            )
            if res.returncode != 0:
                raise AutomationError("Could not read Windows clipboard")
            text = res.stdout
        else:
            for cmd in (["xclip", "-selection", "clipboard", "-o"], ["xsel", "--clipboard", "--output"], ["wl-paste", "-n"]):
                exe = shutil.which(cmd[0])
                if exe:
                    res = subprocess.run(
                        [exe, *cmd[1:]], capture_output=True, text=True, timeout=3.0, check=False, shell=False
                    )
                    if res.returncode == 0:
                        text = res.stdout
                        break
            else:
                raise AutomationError("No local clipboard utility available on this OS")

        try:
            cap = max(1, min(int(max_chars), 20_000))
        except (TypeError, ValueError, OverflowError) as exc:
            raise AutomationError("Clipboard max_chars must be a finite integer") from exc
        full_length = len(text or "")
        # Redaction and secret scanning used to run on an unbounded clipboard
        # string before the result was sliced. Bound the untrusted input first.
        bounded_text = (text or "")[:cap]
        had_secrets = contains_secrets(bounded_text)
        redacted = redact_text(bounded_text)
        return {
            "content": redacted,
            "length": full_length,
            "truncated": full_length > cap,
            "secrets_redacted": had_secrets,
            "untrusted_clipboard": True,
        }

    def write_clipboard(self, text: str) -> dict[str, Any]:
        if not isinstance(text, str):
            raise AutomationError("Clipboard text must be a string")
        if len(text) > 50_000:
            raise AutomationError("Clipboard write exceeds 50,000 character limit")
        if os.name == "nt" and self._user32 is None:
            res = subprocess.run(
                ["clip"], input=text, text=True, capture_output=True, timeout=4.0, check=False, shell=False
            )
            if res.returncode != 0:
                raise AutomationError("Could not write to Windows clipboard")
            return {"written_chars": len(text)}

        for cmd in (["xclip", "-selection", "clipboard"], ["xsel", "--clipboard", "--input"], ["wl-copy"]):
            exe = shutil.which(cmd[0])
            if exe:
                res = subprocess.run(
                    [exe, *cmd[1:]], input=text, text=True, capture_output=True, timeout=3.0, check=False, shell=False
                )
                if res.returncode == 0:
                    return {"written_chars": len(text)}
        raise AutomationError("No local clipboard utility available on this OS")

    def capture_screen(self, output_path: str) -> dict[str, Any]:
        if self.files is None:
            raise AutomationError("File manager required to confine screen capture destination")
        dest = self.files.resolve(output_path, must_exist=False)
        if dest.suffix.lower() != ".png":
            raise AutomationError("Screen capture destination must end in .png")
        dest.parent.mkdir(parents=True, exist_ok=True)

        # Try Pillow ImageGrab if installed locally
        try:
            from PIL import ImageGrab  # type: ignore[import-untyped]

            shot = ImageGrab.grab()
            shot.save(dest, format="PNG")
            return {"path": str(dest), "bytes": dest.stat().st_size, "backend": "pillow_imagegrab"}
        except Exception:
            pass

        # Try PySide6 primaryScreen grabWindow if Qt GUI is active
        try:
            from PySide6.QtGui import QGuiApplication

            screen = QGuiApplication.primaryScreen()
            if screen is not None:
                pix = screen.grabWindow(0)
                if pix.save(str(dest), "PNG"):
                    return {"path": str(dest), "bytes": dest.stat().st_size, "backend": "qt_primary_screen"}
        except Exception:
            pass

        raise AutomationError(
            "Screen capture requires an active desktop session with PySide6 or Pillow installed locally"
        )

    def list_processes(self, *, filter_name: str = "", limit: int = 25) -> dict[str, Any]:
        needle = filter_name.strip().casefold()
        cap = max(1, min(limit, 100))
        procs: list[dict[str, Any]] = []

        if os.name == "nt":
            res = subprocess.run(
                ["tasklist", "/FO", "CSV", "/NH"],
                capture_output=True,
                text=True,
                timeout=6.0,
                check=False,
                shell=False,
            )
            reader = csv.reader(io.StringIO(res.stdout))
            for row in reader:
                if len(row) < 5:
                    continue
                name, pid_s, _sess, _snum, mem_s = row[:5]
                if needle and needle not in name.casefold():
                    continue
                digits = "".join(ch for ch in mem_s if ch.isdigit())
                mem_mb = round(int(digits) / 1024.0, 2) if digits else 0.0
                procs.append(
                    {
                        "pid": int(pid_s) if pid_s.isdigit() else 0,
                        "name": name[:120],
                        "rss_mb": mem_mb,
                    }
                )
        elif Path("/proc").is_dir():
            page_kb = 4
            for entry in Path("/proc").iterdir():
                if not entry.name.isdigit():
                    continue
                try:
                    comm = (entry / "comm").read_text(encoding="utf-8", errors="replace").strip()
                    if needle and needle not in comm.casefold():
                        continue
                    statm = (entry / "statm").read_text(encoding="ascii").split()
                    rss_pages = int(statm[1]) if len(statm) > 1 else 0
                    rss_mb = round((rss_pages * page_kb) / 1024.0, 2)
                    procs.append({"pid": int(entry.name), "name": comm[:120], "rss_mb": rss_mb})
                except OSError:
                    continue

        procs.sort(key=lambda p: (-float(p["rss_mb"]), p["name"]))
        return {"count": min(len(procs), cap), "total_scanned": len(procs), "processes": procs[:cap]}

    def power_control(self, action: str, *, dry_run: bool = False) -> dict[str, Any]:
        clean = action.strip().lower()
        if clean not in POWER_ACTIONS:
            raise AutomationError(f"Unsupported power action '{action}'; choose from {sorted(POWER_ACTIONS)}")
        if dry_run:
            return {"action": clean, "executed": False, "dry_run": True}

        if os.name == "nt" or self._user32 is not None:
            if clean == "lock":
                u32 = self._win_user32()
                if not u32.LockWorkStation():
                    raise AutomationError("LockWorkStation failed")
                return {"action": "lock", "executed": True}
            # Sleep / hibernate via powrprof SetSuspendState
            import ctypes

            windll = getattr(ctypes, "windll", None)
            powr = getattr(windll, "powrprof", None) if windll is not None else None
            if powr is None:
                raise AutomationError("Windows powrprof.dll is unavailable")
            hibernate_flag = 1 if clean == "hibernate" else 0
            powr.SetSuspendState(hibernate_flag, 0, 0)
            return {"action": clean, "executed": True}

        loginctl = shutil.which("loginctl")
        if loginctl:
            subcmd = {"lock": "lock-session", "sleep": "suspend", "hibernate": "hibernate"}[clean]
            res = subprocess.run(
                [loginctl, subcmd], capture_output=True, text=True, timeout=4.0, check=False, shell=False
            )
            if res.returncode == 0:
                return {"action": clean, "executed": True}
        raise AutomationError(f"Power action '{clean}' requires Windows 11 or systemd-logind")


def build_executive_briefing(
    *,
    hardware: dict[str, Any],
    tasks_summary: dict[str, Any],
    playbooks: list[dict[str, Any]],
    scheduled_jobs: list[dict[str, Any]],
    audit_integrity: dict[str, Any],
    override_status: dict[str, Any],
    offline_status: dict[str, Any],
    knowledge_stats: dict[str, Any],
    top_processes: list[dict[str, Any]],
) -> dict[str, Any]:
    """Synthesize a J.A.R.V.I.S.-style situational awareness briefing from 100% local telemetry."""
    mem = hardware.get("memory", {})
    pwr = hardware.get("power", {})
    rec = hardware.get("recommended_profile", {})
    counts = tasks_summary.get("counts_by_state", {})
    awaiting = int(counts.get("AWAITING_APPROVAL", 0))
    verifying = int(counts.get("VERIFYING", 0))
    running = int(counts.get("RUNNING", 0))

    posture = (
        "RED ALERT LOCKDOWN"
        if override_status.get("lockdown_active")
        else (
            f"COMMANDER LEASE ACTIVE ({override_status.get('commander_remaining_seconds', 0):.0f}s remaining)"
            if override_status.get("commander_mode_active")
            else "STANDARD GUARDED AUTONOMY"
        )
    )
    power_line = (
        f"On Battery ({pwr.get('percent')}%)"
        if pwr.get("on_battery")
        else "AC Mains Connected"
    )
    audit_line = "INTACT" if audit_integrity.get("intact") else "WARNING: HASH CHAIN MISMATCH"

    lines = [
        f"TACTICAL BRIEFING — Posture: {posture}",
        f"• Compute & Power: {hardware.get('logical_cpus', '?')} logical cores | RAM {mem.get('available_gb', '?')}/{mem.get('total_gb', '?')} GB free | {power_line} | Profile: {rec.get('profile_name', 'auto')}",
        f"• Network & Audit: Mode={offline_status.get('mode', 'offline')} | SHA-256 Trace Chain={audit_line} ({audit_integrity.get('checked_events', 0)} events verified)",
        f"• Operations Queue: {running} running, {awaiting} awaiting approval, {verifying} awaiting evidence review | {len(scheduled_jobs)} scheduled daemon(s) | {len(playbooks)} saved playbook(s)",
        f"• Local Knowledge: {knowledge_stats.get('documents', 0)} docs, {knowledge_stats.get('chunks', 0)} chunks, {knowledge_stats.get('stored_vectors', 0)} vectors (ANN={knowledge_stats.get('vector_ann', {}).get('backend', 'ready')})",
    ]
    if top_processes:
        top3 = ", ".join(f"{p['name']} ({p['rss_mb']} MB)" for p in top_processes[:3])
        lines.append(f"• Top Memory Consumers: {top3}")

    return {
        "posture": posture,
        "summary_text": "\n".join(lines),
        "awaiting_approvals": awaiting,
        "awaiting_verification": verifying,
        "running_tasks": running,
        "scheduled_jobs_count": len(scheduled_jobs),
        "playbooks_count": len(playbooks),
        "audit_intact": bool(audit_integrity.get("intact")),
        "override": override_status,
        "top_processes": top_processes[:5],
    }
