"""Small Windows SendInput helpers for text and hardware volume keys."""

from __future__ import annotations

from typing import Any

from hinata.automation.app_control import AutomationError


class WindowsControl:
    @staticmethod
    def _require_windows() -> None:
        import os

        if os.name != "nt":
            raise AutomationError("This Windows control is available only on Windows 11")

    def volume(self, action: str, value: int | None = None) -> dict[str, Any]:
        self._require_windows()
        import ctypes

        user32 = ctypes.windll.user32  # type: ignore[attr-defined]
        keys = {"up": 0xAF, "down": 0xAE, "mute": 0xAD}
        if action in keys:
            key = keys[action]
            user32.keybd_event(key, 0, 0, 0)
            user32.keybd_event(key, 0, 2, 0)
            return {"action": action, "note": "Hardware volume key sent; exact level is device-dependent"}
        if action == "set" and value is not None:
            try:
                from comtypes import CLSCTX_ALL, POINTER, cast
                from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume

                device = AudioUtilities.GetSpeakers()
                interface = device.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
                endpoint = cast(interface, POINTER(IAudioEndpointVolume))
                endpoint.SetMasterVolumeLevelScalar(value / 100.0, None)
                actual = round(float(endpoint.GetMasterVolumeLevelScalar()) * 100)
                return {"action": "set", "value": actual}
            except ImportError as exc:
                raise AutomationError(
                    "Exact volume setting needs the optional 'pycaw' package: python -m pip install pycaw"
                ) from exc
            except Exception as exc:
                raise AutomationError(f"Windows audio endpoint rejected the change: {exc}") from exc
        raise AutomationError("Unsupported volume action")

    def active_window_title(self) -> str:
        self._require_windows()
        import ctypes

        user32 = ctypes.windll.user32  # type: ignore[attr-defined]
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return ""
        length = user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return ""
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        return str(buf.value or "")

    def type_text(self, text: str, *, target_window: str = "") -> dict[str, Any]:
        self._require_windows()
        import ctypes
        import time
        from ctypes import wintypes

        user32 = ctypes.windll.user32  # type: ignore[attr-defined]
        ulong_ptr = wintypes.WPARAM

        class KEYBDINPUT(ctypes.Structure):
            _fields_ = [
                ("wVk", wintypes.WORD), ("wScan", wintypes.WORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("dwExtraInfo", ulong_ptr),
            ]

        class MOUSEINPUT(ctypes.Structure):
            _fields_ = [
                ("dx", wintypes.LONG), ("dy", wintypes.LONG),
                ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ulong_ptr),
            ]

        class INPUTUNION(ctypes.Union):
            _fields_ = [("ki", KEYBDINPUT), ("mi", MOUSEINPUT)]

        class INPUT(ctypes.Structure):
            _anonymous_ = ("union",)
            _fields_ = [("type", wintypes.DWORD), ("union", INPUTUNION)]

        def _check_focus(initial_hwnd: int) -> None:
            current_hwnd = user32.GetForegroundWindow()
            if initial_hwnd and current_hwnd != initial_hwnd:
                raise AutomationError("Active window focus changed during typing; cancelled safely.")
            if target_window.strip():
                title = self.active_window_title()
                if target_window.strip().casefold() not in title.casefold():
                    raise AutomationError(
                        f"Foreground window '{title}' does not match required target_window '{target_window}'; typing cancelled."
                    )

        def send(virtual_key: int, scan: int, flags: int) -> None:
            event = INPUT(type=1, ki=KEYBDINPUT(virtual_key, scan, flags, 0, 0))
            if user32.SendInput(1, ctypes.byref(event), ctypes.sizeof(INPUT)) != 1:
                raise AutomationError("SendInput failed; the active application may have elevated privileges")

        # A short delay lets the user keep focus in the intended application, then verifies target identity.
        time.sleep(0.15)
        initial_hwnd = int(user32.GetForegroundWindow() or 0)
        _check_focus(initial_hwnd)
        normalized = text.replace("\r\n", "\n").replace("\r", "\n")
        for idx, character in enumerate(normalized):
            if idx % 16 == 0:
                _check_focus(initial_hwnd)
            if character == "\n":
                send(0x0D, 0, 0)
                send(0x0D, 0, 2)
            else:
                encoded = character.encode("utf-16-le")
                for offset in range(0, len(encoded), 2):
                    code_unit = int.from_bytes(encoded[offset : offset + 2], "little")
                    send(0, code_unit, 0x0004)
                    send(0, code_unit, 0x0004 | 0x0002)
        return {"characters": len(text), "target_window": target_window or "verified_foreground_hwnd"}
