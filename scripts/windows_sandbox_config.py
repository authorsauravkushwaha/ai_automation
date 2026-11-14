#!/usr/bin/env python3
"""Generate a Windows Sandbox configuration for an offline HINATA instance.

All inputs must already be prepared on the host. This generator does not install
software or launch Windows Sandbox. The generated config disables VM networking.
"""
from __future__ import annotations

import argparse
import sys
import xml.etree.ElementTree as ET
from pathlib import Path


TARGETS = {
    "project": (r"C:\hinata\project", True),
    "python": (r"C:\hinata\python", True),
    "ollama": (r"C:\hinata\ollama", True),
    "models": (r"C:\hinata\models", True),
    "data": (r"C:\hinata\data", False),
    "documents": (r"C:\hinata\documents", True),
}


def build_config(paths: dict[str, str], *, audio: bool = False) -> str:
    if set(paths) != set(TARGETS):
        raise ValueError("All six folder mappings are required")
    root = ET.Element("Configuration")
    ET.SubElement(root, "Networking").text = "Disable"
    ET.SubElement(root, "ClipboardRedirection").text = "Disable"
    ET.SubElement(root, "PrinterRedirection").text = "Disable"
    ET.SubElement(root, "AudioInput").text = "Enable" if audio else "Disable"
    folders = ET.SubElement(root, "MappedFolders")
    for name, (target, read_only) in TARGETS.items():
        folder = ET.SubElement(folders, "MappedFolder")
        ET.SubElement(folder, "HostFolder").text = paths[name]
        ET.SubElement(folder, "SandboxFolder").text = target
        ET.SubElement(folder, "ReadOnly").text = "true" if read_only else "false"
    command = ET.SubElement(root, "LogonCommand")
    ET.SubElement(command, "Command").text = r"cmd.exe /c C:\hinata\project\scripts\windows_sandbox_start.cmd"
    ET.indent(root, space="  ")
    return '<?xml version="1.0" encoding="utf-8"?>\n' + ET.tostring(root, encoding="unicode") + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in TARGETS:
        parser.add_argument("--" + name, required=True, help=f"Existing host {name} directory")
    parser.add_argument("--output", required=True, help="New .wsb file (will not overwrite)")
    parser.add_argument("--enable-audio", action="store_true", help="Allow microphone input in the sandbox")
    args = parser.parse_args(argv)
    if sys.platform != "win32":
        print("Generate and launch this configuration on a Windows Sandbox-capable Windows host.", file=sys.stderr)
        return 1
    paths = {name: Path(getattr(args, name)).expanduser().resolve() for name in TARGETS}
    missing = [name for name, path in paths.items() if not path.is_dir()]
    if missing:
        parser.error("These folders do not exist: " + ", ".join(missing))
    for name, relative in (("project", "scripts/windows_sandbox_start.cmd"), ("python", "python.exe"),
                           ("ollama", "ollama.exe"), ("data", "offline-config.toml")):
        if not (paths[name] / relative).is_file():
            parser.error(f"Missing {relative} in the {name} directory")
    # Overlapping mappings can accidentally make a supposedly read-only tree writable.
    for name, path in paths.items():
        for other_name, other in paths.items():
            if name != other_name and (path == other or path in other.parents or other in path.parents):
                parser.error(f"Mappings must not overlap: {name}, {other_name}")
    output = Path(args.output).expanduser()
    if output.suffix.lower() != ".wsb":
        parser.error("Output must end in .wsb")
    try:
        with output.open("x", encoding="utf-8", newline="\n") as handle:
            handle.write(build_config({key: str(value) for key, value in paths.items()}, audio=args.enable_audio))
    except OSError as exc:
        print(f"Cannot create sandbox config: {exc}", file=sys.stderr)
        return 1
    print(f"Created {output}. Review it, then open it with Windows Sandbox.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
