"""Protect biometric data with Windows DPAPI, with owner-only files elsewhere."""

from __future__ import annotations

import os
from pathlib import Path

from friday.config import restrict_private_path


class ProtectionError(RuntimeError):
    pass


def protect_bytes(data: bytes) -> bytes:
    return _crypt(data, protect=True) if os.name == "nt" else data


def unprotect_bytes(data: bytes) -> bytes:
    return _crypt(data, protect=False) if os.name == "nt" else data


def _crypt(data: bytes, *, protect: bool) -> bytes:
    import ctypes
    from ctypes import wintypes

    class DataBlob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]

    windll = ctypes.windll  # type: ignore[attr-defined]  # Windows-only DPAPI boundary
    input_buffer = ctypes.create_string_buffer(data)
    input_blob = DataBlob(
        len(data), ctypes.cast(input_buffer, ctypes.POINTER(ctypes.c_byte))
    )
    output_blob = DataBlob()
    if protect:
        ok = windll.crypt32.CryptProtectData(
            ctypes.byref(input_blob),
            ctypes.c_wchar_p("FRIDAY owner voice profile"),
            None,
            None,
            None,
            0,
            ctypes.byref(output_blob),
        )
    else:
        ok = windll.crypt32.CryptUnprotectData(
            ctypes.byref(input_blob), None, None, None, None, 0, ctypes.byref(output_blob)
        )
    if not ok:
        operation = "protect" if protect else "decrypt"
        raise ProtectionError(f"Windows DPAPI could not {operation} the local voice profile")
    try:
        return ctypes.string_at(output_blob.pbData, output_blob.cbData)
    finally:
        windll.kernel32.LocalFree(output_blob.pbData)


def write_private(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    restrict_private_path(path.parent)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_bytes(protect_bytes(data))
    if os.name != "nt":
        temporary.chmod(0o600)
    temporary.replace(path)
    restrict_private_path(path)


def read_private(path: Path) -> bytes:
    return unprotect_bytes(path.read_bytes())
