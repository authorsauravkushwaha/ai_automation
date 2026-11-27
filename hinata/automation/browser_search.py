"""Open a search URL in the OS default or a registered Windows browser."""

from __future__ import annotations

import ipaddress
import os
import re
import subprocess
import sys
import webbrowser
from collections.abc import Iterator
from pathlib import Path
from urllib.parse import urlsplit

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
    if not isinstance(name, str):
        raise AutomationError("Browser name must be a string")
    key = " ".join(name.casefold().split()).removesuffix(".exe")
    return _BROWSER_ALIASES.get(key, key)


_UNSAFE_HOST_SUFFIXES = (
    ".local",
    ".localhost",
    ".internal",
    ".test",
    ".invalid",
    ".example",
)


def _validate_browser_url(url: str) -> None:
    """Only hand an ordinary public HTTP(S) URL to an external browser."""
    if (
        not isinstance(url, str)
        or len(url) > 4096
        or any(char in url for char in ("\r", "\n", "\x00"))
    ):
        raise AutomationError("Browser URL must be a short HTTP(S) URL without control characters")
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError as exc:
        raise AutomationError("Browser URL has an invalid port") from exc
    host = (parsed.hostname or "").rstrip(".").lower()
    if (
        parsed.scheme not in {"http", "https"}
        or not host
        or parsed.username
        or parsed.password
        or (port is not None and not 1 <= port <= 65535)
        or host == "localhost"
        or host.endswith(_UNSAFE_HOST_SUFFIXES)
    ):
        raise AutomationError("Browser URL must be a public HTTP(S) URL without credentials")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return
    if (
        not address.is_global
        or address.is_loopback
        or address.is_link_local
        or address.is_private
        or address.is_reserved
        or address.is_multicast
        or address.is_unspecified
    ):
        raise AutomationError("Browser URL must not target a private or special-use IP address")


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
    _validate_browser_url(url)
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
