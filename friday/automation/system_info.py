"""Basic local system information without a heavy metrics dependency."""

from __future__ import annotations

import os
import platform
import shutil
import sys
from pathlib import Path
from typing import Any


class SystemInfo:
    def snapshot(self) -> dict[str, Any]:
        memory = self._memory()
        disk = shutil.disk_usage(Path.home())
        return {
            "os": platform.platform(),
            "python": sys.version.split()[0],
            "processor": platform.processor() or platform.machine(),
            "logical_cpus": os.cpu_count() or 1,
            "memory": memory,
            "home_disk": {
                "total_gb": round(disk.total / 1024**3, 1),
                "free_gb": round(disk.free / 1024**3, 1),
            },
        }

    @staticmethod
    def _memory() -> dict[str, Any]:
        if os.name == "nt":
            import ctypes

            class MemoryStatus(ctypes.Structure):
                _fields_ = [
                    ("length", ctypes.c_ulong),
                    ("memory_load", ctypes.c_ulong),
                    ("total_physical", ctypes.c_ulonglong),
                    ("available_physical", ctypes.c_ulonglong),
                    ("total_page_file", ctypes.c_ulonglong),
                    ("available_page_file", ctypes.c_ulonglong),
                    ("total_virtual", ctypes.c_ulonglong),
                    ("available_virtual", ctypes.c_ulonglong),
                    ("available_extended_virtual", ctypes.c_ulonglong),
                ]

            status = MemoryStatus()
            status.length = ctypes.sizeof(MemoryStatus)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):  # type: ignore[attr-defined]
                return {
                    "total_gb": round(status.total_physical / 1024**3, 1),
                    "available_gb": round(status.available_physical / 1024**3, 1),
                    "used_percent": status.memory_load,
                }
        try:
            pages = os.sysconf("SC_PHYS_PAGES")  # type: ignore[attr-defined]
            available = os.sysconf("SC_AVPHYS_PAGES")  # type: ignore[attr-defined]
            page_size = os.sysconf("SC_PAGE_SIZE")  # type: ignore[attr-defined]
            return {
                "total_gb": round(pages * page_size / 1024**3, 1),
                "available_gb": round(available * page_size / 1024**3, 1),
            }
        except (ValueError, OSError, AttributeError):
            return {"available": False}
