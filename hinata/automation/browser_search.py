"""Open a search URL in the OS default or a registered Windows browser."""

from __future__ import annotations

import os
import re
import subprocess
import sys
import webbrowser
from collections.abc import Iterator
from pathlib import Path

from hinata.automation.app_control import AutomationError

_BROWSER_ALIASES = {
    "google chrome": "chrome",
    "microsoft edge": "edge",
    "msedge": "edge",
    "mozilla firefox": "firefox",
    "brave browser": "brave",
    "opera browser": "opera",
    "vivaldi browser": "vivaldi",
}


def browser_key(name: str) -> str:
    key = " ".join(name.casefold().split()).removesuffix(".exe")
    return _BROWSER_ALIASES.get(key, key)


def _registered_browsers() -> Iterator[tuple[str, str, Path]]:
    if sys.platform != "win32":
        return
    import winreg

    root = r"SOFTWARE\Clients\StartMenuInternet"
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        for view in (0, winreg.KEY_WOW64_32KEY, winreg.KEY_WOW64_64KEY):
            try:
                with winreg.OpenKey(hive, root, 0, winreg.KEY_READ | view) as registry:
                    for index in range(winreg.QueryInfoKey(registry)[0]):
                        try:
                            client = winreg.EnumKey(registry, index)
                            command = winreg.QueryValue(registry, client + r"\shell\open\command")
                        except OSError:
                            continue
                        try:
                            label = winreg.QueryValue(registry, client) or client
                        except OSError:
                            label = client
                        # Use only the executable of the registered browser, not registry flags.
                        match = re.match(r'^\s*(?:"([^"]+\.exe)"|([^"\r\n]+?\.exe))(?=\s|$)', command, re.I)
                        if match:
                            exe = Path(os.path.expandvars(match[1] or match[2]))
                            if exe.is_absolute() and exe.is_file():
                                yield client, label, exe
            except OSError:
                continue


def open_browser_search(url: str, browser: str = "default") -> str:
    """Open a caller-built fixed-provider URL; return the browser handed the request."""
    key = browser_key(browser)
    try:
        if key in {"default", "browser", "default browser", "my browser"}:
            if sys.platform == "win32":
                os.startfile(url)
            elif not webbrowser.open(url, new=2):
                raise AutomationError("Could not open the default browser. Set it in your system settings.")
            return "your default browser"
        if os.name == "nt":
            for client, label, exe in _registered_browsers():
                if key in {browser_key(name) for name in (client, label, exe.stem)}:
                    subprocess.Popen([str(exe), url], shell=False, close_fds=True)
                    return label
    except (OSError, webbrowser.Error) as exc:
        raise AutomationError(f"Could not open the browser: {exc}") from exc
    raise AutomationError(
        f"Browser '{browser}' was not found among the registered Windows browsers. "
        "Set it as your system default and try 'search in browser for …'."
    )
