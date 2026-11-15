"""Tests for J.A.R.V.I.S. tactical cockpit, window/hotkey/clipboard/process/power tools,
executive situational briefing, Jarvis personality mode, Red Alert Lockdown, and Commander Lease.
"""

from __future__ import annotations

import ctypes
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from hinata.automation.app_control import AutomationError
from hinata.automation.file_manager import FileManager
from hinata.automation.jarvis_cockpit import DesktopCockpit, JarvisOverrideController
from hinata.config import SecurityConfig
from tests.helpers import make_assistant


class JarvisCockpitAndProtocolsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_jarvis_personality_and_executive_briefing_end_to_end(self) -> None:
        assistant = make_assistant(self.tmp.name)
        self.addCleanup(assistant.close)

        # Switch into Jarvis Tactical personality mode
        mode_res = assistant.process_text("switch to jarvis mode")
        self.assertTrue(mode_res.success)
        self.assertEqual(assistant.personality.mode.key, "jarvis")

        # Run executive briefing deterministically offline
        brief = assistant.process_text("brief me")
        self.assertTrue(brief.success)
        self.assertIn("TACTICAL BRIEFING", brief.text)
        self.assertIn("STANDARD GUARDED AUTONOMY", brief.text)
        self.assertIn("SHA-256 Trace Chain=INTACT", brief.text)

        # Live process inspection runs natively on Linux /proc
        procs = assistant.process_text("list top processes")
        self.assertTrue(procs.success)
        self.assertIn("Top", procs.text)

    def test_red_alert_lockdown_and_pin_commander_lease_governance(self) -> None:
        assistant = make_assistant(self.tmp.name)
        self.addCleanup(assistant.close)
        assistant.authentication.set_pin("842691")

        # 1. Engage Red Alert Lockdown -> freezes mutating and file-create tools immediately
        st = assistant.engage_lockdown("Simulated perimeter breach")
        self.assertTrue(st["lockdown_active"])

        blocked_create = assistant.process_text(f"create folder {self.root / 'denied_dir'}")
        self.assertFalse(blocked_create.success)
        self.assertEqual(blocked_create.status, "denied")
        self.assertIn("RED ALERT LOCKDOWN", blocked_create.text)
        self.assertFalse((self.root / "denied_dir").exists())

        # Situational briefing still works while locked down
        brief = assistant.process_text("executive briefing")
        self.assertTrue(brief.success)
        self.assertIn("RED ALERT LOCKDOWN", brief.text)

        # Wrong PIN cannot lift lockdown
        with self.assertRaises(PermissionError):
            assistant.release_lockdown("000000")
        self.assertTrue(assistant.override_controller.is_lockdown)

        # Verified Owner PIN lifts lockdown
        lifted = assistant.release_lockdown("842691")
        self.assertFalse(lifted["lockdown_active"])

        # 2. Without Commander Mode, CONFIRM-tier 'remember_preference' asks for confirmation
        pref_prompt = assistant.process_text("remember that my suit_mark is Mark_LXXXV")
        self.assertTrue(pref_prompt.needs_confirmation)

        # Activate PIN-verified Commander Lease (10 min) -> CONFIRM-tier executes cleanly without extra dialog!
        lease = assistant.activate_commander_mode("842691", minutes=10, label="Workshop Build")
        self.assertTrue(lease["commander_mode_active"])

        pref_exec = assistant.process_text("remember that my suit_mark is Mark_LXXXV")
        self.assertTrue(pref_exec.success)

        # CRITICAL SAFETY INVARIANT: Even in Commander Mode, SENSITIVE destructive tools STILL require confirmation + PIN!
        wipe = assistant.process_text("clear memory")
        self.assertFalse(wipe.success)
        self.assertTrue(wipe.needs_confirmation)

        # Stand down revokes Commander Mode in 0 ms without needing PIN
        stood_down = assistant.stand_down_commander_mode()
        self.assertFalse(stood_down["commander_mode_active"])
        pref_again = assistant.process_text("remember that my suit_mark is Mark_XLII")
        self.assertTrue(pref_again.needs_confirmation)

    def test_win32_window_cockpit_focus_snap_and_safe_hotkeys(self) -> None:
        fake_u32 = MagicMock()
        fake_u32.GetForegroundWindow.return_value = 101
        fake_u32.IsWindowVisible.return_value = 1
        fake_u32.IsIconic.return_value = 0
        fake_u32.GetSystemMetrics.side_effect = lambda idx: 1920 if idx == 0 else 1080

        title_text = "Visual Studio Code - ai_automation"

        def _fake_len(hwnd: int) -> int:
            return len(title_text) if hwnd == 101 else 0

        def _fake_get_text(hwnd: int, buf, _max_len: int) -> int:
            if hwnd == 101:
                buf.value = title_text
                return len(title_text)
            return 0

        def _fake_pid(_hwnd: int, pid_ptr) -> int:
            ctypes.cast(pid_ptr, ctypes.POINTER(ctypes.c_ulong))[0] = 7777
            return 1

        def _fake_enum(cb, _lparam: int) -> int:
            cb(101, 0)
            return 1

        fake_u32.GetWindowTextLengthW.side_effect = _fake_len
        fake_u32.GetWindowTextW.side_effect = _fake_get_text
        fake_u32.GetWindowThreadProcessId.side_effect = _fake_pid
        fake_u32.EnumWindows.side_effect = _fake_enum

        files = FileManager(SecurityConfig(allowed_roots=[self.root]))
        # ctypes.WINFUNCTYPE is Windows-only; patch it portably on Linux so Win32 callback logic runs 100%
        with patch.object(ctypes, "WINFUNCTYPE", side_effect=lambda *_args: lambda fn: fn, create=True):
            cockpit = DesktopCockpit(files, user32=fake_u32)

            listed = cockpit.list_windows(filter_text="studio")
            self.assertEqual(listed["count"], 1)
            self.assertEqual(listed["windows"][0]["pid"], 7777)

            focused = cockpit.focus_window("Visual Studio")
            self.assertTrue(focused["focused"])
            fake_u32.SetForegroundWindow.assert_called_with(101)

            snapped = cockpit.snap_window("Visual Studio", "left")
            self.assertTrue(snapped["applied"])
            fake_u32.MoveWindow.assert_called_with(101, 0, 0, 960, 1080, True)

            hk = cockpit.send_hotkey("save", target_window="Visual Studio")
            self.assertTrue(hk["sent"])
            self.assertGreaterEqual(fake_u32.keybd_event.call_count, 4)

            # Dangerous unapproved hotkeys (e.g. alt+f4) are strictly rejected
            with self.assertRaises(AutomationError):
                cockpit.send_hotkey("alt+f4")

            # Lock workstation calls LockWorkStation
            fake_u32.LockWorkStation.return_value = 1
            locked = cockpit.power_control("lock")
            self.assertTrue(locked["executed"])
            fake_u32.LockWorkStation.assert_called_once()

    def test_clipboard_secret_redaction_and_intent_parsing(self) -> None:
        files = FileManager(SecurityConfig(allowed_roots=[self.root]))
        cockpit = DesktopCockpit(files)

        fake_cp = subprocess.CompletedProcess(
            args=["xclip"],
            returncode=0,
            stdout="Token sk-proj-ABCDEFGHIJKLMNOPQRSTUVWXYZ1234567890 inside clipboard",
            stderr="",
        )
        with patch("hinata.automation.jarvis_cockpit.shutil.which", return_value="/usr/bin/xclip"), patch(
            "hinata.automation.jarvis_cockpit.subprocess.run", return_value=fake_cp
        ):
            clip = cockpit.read_clipboard()
            self.assertTrue(clip["secrets_redacted"])
            self.assertNotIn("ABCDEFGHIJKLMNOPQRSTUVWXYZ1234567890", clip["content"])

        ov = JarvisOverrideController()
        self.assertFalse(ov.is_commander_active)
        with self.assertRaises(PermissionError):
            ov.activate_commander_lease(pin_verified=False, minutes=5)


if __name__ == "__main__":
    unittest.main()
