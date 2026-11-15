"""Explicit, reversible application of an owner-selected local model fleet."""

from __future__ import annotations

import json
import os
import re
import tempfile
import tomllib
from pathlib import Path

from hinata.config import load_config
from hinata.core.jarvis_models import FleetPack


def apply_fleet_pack(config_path: Path, pack: FleetPack) -> Path:
    """Update only [ollama] role settings; retain all other config and back it up.

    Called only after explicit confirmation and successful model pulls, never from
    status/detection or normal inference. On validation failure, leave the original.
    """
    path = config_path.expanduser().resolve()
    original = path.read_text(encoding="utf-8")
    newline = "\r\n" if "\r\n" in original else "\n"
    data = tomllib.loads(original)
    if not isinstance(data.get("ollama", {}), dict):
        raise ValueError("Invalid [ollama] section")
    values: dict[str, str | int | bool] = {
        "enabled": True,
        "model": pack.chat_model,
        "planner_model": pack.planner_model,
        "coding_model": pack.coding_model,
        "reviewer_model": pack.reviewer_model,
        "vision_model": pack.vision_model,
        "embedding_model": pack.embedding_model,
        "hardware_profile": "lenovo_loq_i5",
        "context_tokens": pack.recommended_context_tokens,
        "max_tokens": pack.recommended_max_tokens,
        "threads": pack.recommended_threads,
    }
    # Preserve comments and unrelated fields even when [ollama] is not first.
    lines = original.splitlines(keepends=True)
    section_start = next(
        (i for i, line in enumerate(lines) if re.fullmatch(r"\s*\[ollama\]\s*", line.strip())),
        None,
    )
    if section_start is None:
        lines.extend([newline, f"[ollama]{newline}"])
        section_start = len(lines) - 1
    section_end = next(
        (i for i in range(section_start + 1, len(lines)) if re.match(r"\s*\[", lines[i])),
        len(lines),
    )
    seen: set[str] = set()
    for index in range(section_start + 1, section_end):
        match = re.match(r"^(\s*)([a-zA-Z_][a-zA-Z_0-9]*)\s*=", lines[index])
        if match and match.group(2) in values:
            key = match.group(2)
            lines[index] = f"{match.group(1)}{key} = {json.dumps(values[key])}{newline}"
            seen.add(key)
    missing = [f"{key} = {json.dumps(val)}{newline}" for key, val in values.items() if key not in seen]
    lines[section_end:section_end] = missing
    updated = "".join(lines)
    tomllib.loads(updated)
    fd, temp_path = tempfile.mkstemp(prefix=".hinata-config-", suffix=".toml", dir=path.parent)
    os.close(fd)
    candidate = Path(temp_path)
    try:
        candidate.write_text(updated, encoding="utf-8", newline="")
        load_config(candidate)  # Validate all sections before touching the existing file.
        base_backup = path.with_suffix(path.suffix + ".bak")
        for suffix in ["", *[f".{i}" for i in range(1, 101)]]:
            backup = Path(str(base_backup) + suffix)
            try:
                flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
                fd_backup = os.open(backup, flags, 0o600)
            except FileExistsError:
                continue
            with os.fdopen(fd_backup, "w", encoding="utf-8", newline="") as stream:
                stream.write(original)
            os.replace(candidate, path)
            return backup
        raise OSError("No free config backup slot; refusing to overwrite a backup")
    finally:
        candidate.unlink(missing_ok=True)
