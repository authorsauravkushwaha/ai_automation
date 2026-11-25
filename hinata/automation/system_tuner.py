"""System Tuner & Guardian — health, cleanup, backup, diagnostics.

All operations are local, bounded, and permission-gated.
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path
from typing import Any

from hinata.automation.app_control import AutomationError
from hinata.automation.system_info import SystemInfo


class SystemTuner:
    def __init__(self, files=None, system: SystemInfo | None = None) -> None:
        self.files = files
        self.system = system or SystemInfo()

    def health_check(self) -> dict[str, Any]:
        sys_data = self.system.snapshot()
        issues: list[str] = []
        ok: list[str] = []
        # Disk check
        try:
            disks = sys_data.get("disks", [])
            for d in disks:
                if isinstance(d, dict):
                    pct = d.get("percent", 0)
                    if pct > 90:
                        issues.append(f"Disk {d.get('mount','')} is {pct}% full")
                    elif pct > 75:
                        issues.append(f"Disk {d.get('mount','')} is {pct}% full (warning)")
                    else:
                        ok.append(f"Disk {d.get('mount','')} OK ({pct}%)")
        except Exception:
            pass
        # Memory
        try:
            mem = sys_data.get("memory", {})
            if isinstance(mem, dict):
                pct = mem.get("percent", 0)
                if pct > 85:
                    issues.append(f"Memory {pct}% used — consider closing apps")
                else:
                    ok.append(f"Memory {pct}%")
        except Exception:
            pass
        # CPU
        try:
            cpu = sys_data.get("cpu", {})
            if isinstance(cpu, dict):
                ok.append(f"CPU: {cpu.get('model','unknown')[:60]} x{cpu.get('cores', '?')} cores")
        except Exception:
            pass
        status = "healthy" if not issues else "needs_attention"
        return {"status": status, "issues": issues, "ok": ok, "snapshot": sys_data}

    def network_diag(self) -> dict[str, Any]:
        """Probe connectivity, starting with loopback and only then the internet.

        Ordering matters: under ``strict_offline`` the loopback probe still
        answers, and every external probe is *attempted* through the same socket
        layer the rest of the app uses, so the offline guard records and blocks
        it rather than this diagnostic silently bypassing the policy.
        """
        checks: list[dict[str, Any]] = []
        local = self._probe_connection("127.0.0.1", 1)
        checks.append(local)
        for host, port in (("1.1.1.1", 80), ("8.8.8.8", 80), ("example.com", 80)):
            checks.append(self._probe_connection(host, port))
        online = any(c.get("ok") for c in checks if c["host"] != "127.0.0.1")
        loopback_ok = bool(checks[0].get("ok"))
        return {
            "online": online,
            "loopback": loopback_ok,
            "checks": checks,
            "note": (
                "Loopback is always checked first. External probes are blocked by "
                "strict_offline / the online-mode allowlist, so 'online: false' can "
                "mean 'policy blocked' rather than 'no network'."
            ),
        }

    @staticmethod
    def _probe_connection(host: str, port: int) -> dict[str, Any]:
        import socket

        target: str
        try:
            target = host if host[0].isdigit() else socket.gethostbyname(host)
        except OSError as exc:
            return {"host": host, "ok": False, "error": f"dns: {exc}"[:120]}
        try:
            start = time.time()
            socket.create_connection((target, port), timeout=2.0).close()
            return {"host": host, "ok": True, "ms": round((time.time() - start) * 1000, 1)}
        except OSError as exc:
            return {"host": host, "ok": False, "error": str(exc)[:120]}

    def backup_create(self, source: str, dest_dir: str, *, max_age_days: int = 30) -> dict[str, Any]:
        if not self.files:
            raise AutomationError("File manager not required for backup")
        src = self.files.resolve(source)
        dst_root = self.files.resolve(dest_dir, must_exist=False)
        dst_root.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d_%H%M%S")
        dst = dst_root / f"backup_{src.name}_{stamp}.zip"
        if dst.exists():
            raise AutomationError(f"Backup dest exists: {dst}")
        # Use zipfile
        import zipfile
        count = 0
        total = 0
        with zipfile.ZipFile(dst, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            if src.is_file():
                zf.write(src, arcname=src.name)
                count = 1
                total = src.stat().st_size
            else:
                # Prune the walk so the archive never contains itself: if the
                # destination sits inside the source (the common "back up into
                # Documents/Backups" case), every run would otherwise nest the
                # previous backups inside the new one, growing without bound.
                src_resolved = src.resolve()
                dst_resolved = dst_root.resolve()
                excluded: Path | None = None
                try:
                    if dst_resolved == src_resolved or dst_resolved.is_relative_to(src_resolved):
                        excluded = dst_resolved
                except (OSError, ValueError):
                    excluded = None
                for dirpath, dirnames, filenames in os.walk(src):
                    if excluded is not None:
                        dirnames[:] = [
                            d for d in dirnames if (Path(dirpath) / d).resolve() != excluded
                        ]
                    for fn in filenames:
                        if fn.startswith("."):
                            continue
                        fp = Path(dirpath) / fn
                        if fp.is_symlink():
                            continue
                        try:
                            rel = fp.relative_to(src)
                            zf.write(fp, arcname=rel.as_posix())
                            count += 1
                            total += fp.stat().st_size
                            if count >= 500 or total > 200 * 1024 * 1024:
                                raise AutomationError("Backup exceeds 500 files or 200 MB limit")
                        except OSError:
                            continue
        # Prune old backups
        pruned = 0
        cutoff = time.time() - max_age_days * 86400
        for item in dst_root.glob(f"backup_{src.name}_*.zip"):
            try:
                if item.stat().st_mtime < cutoff:
                    item.unlink()
                    pruned += 1
            except OSError:
                pass
        return {"source": str(src), "archive": str(dst), "files": count, "bytes": total, "pruned_old": pruned}

    def quick_cleanup(self, path: str = "", *, dry_run: bool = True, min_age_days: int = 1) -> dict[str, Any]:
        if not self.files:
            raise AutomationError("File manager needed")
        if path.strip():
            root = self.files.resolve(path)
        else:
            root = next((r for r in self.files.roots if r.exists()), self.files.roots[0] if self.files.roots else Path.cwd())
        if not root.is_dir():
            raise AutomationError(f"Not a directory: {root}")
        # Find empty folders + tmp/log/bak files.
        # ``min_age_days`` protects files still in use: a log written seconds ago
        # (or a download in progress) must not be swept up by a "cleanup".
        cutoff = time.time() - max(0, min_age_days) * 86400
        empty_dirs: list[str] = []
        temp_files: list[dict[str, Any]] = []
        for dirpath, dirnames, filenames in os.walk(root):
            if not dirnames and not filenames:
                empty_dirs.append(dirpath)
            for fn in filenames:
                low = fn.lower()
                if not (low.endswith((".tmp", ".temp", ".bak", ".cache", ".log")) or fn.startswith("~")):
                    continue
                fp = Path(dirpath) / fn
                try:
                    if fp.is_symlink():
                        continue  # never delete through a link into another folder
                    stat = fp.stat()
                    if stat.st_mtime >= cutoff:
                        continue  # too recent: possibly still being written
                    temp_files.append(
                        {"path": str(fp), "bytes": stat.st_size,
                         "age_days": round((time.time() - stat.st_mtime) / 86400, 1)}
                    )
                except OSError:
                    pass
        total_bytes = sum(x["bytes"] for x in temp_files)
        if not dry_run:
            for d in empty_dirs:
                try:
                    Path(d).rmdir()
                except OSError:
                    pass
            for f in temp_files:
                try:
                    Path(f["path"]).unlink()
                except OSError:
                    pass
        return {
            "root": str(root),
            "empty_dirs": empty_dirs[:50],
            "empty_count": len(empty_dirs),
            "temp_files": temp_files[:50],
            "temp_count": len(temp_files),
            "temp_mb": round(total_bytes / (1024 * 1024), 2),
            "dry_run": dry_run,
        }

    def process_killer(self, name: str, *, dry_run: bool = True) -> dict[str, Any]:
        """Find processes by name (read-only scan + optional kill with confirmation)."""
        if not name or len(name) < 2:
            raise AutomationError("Process name must be at least 2 chars")
        # Use ps/tasklist via DesktopCockpit logic inline for portability
        procs: list[dict[str, Any]] = []
        if os.name == "nt":
            try:
                out = subprocess.check_output(["tasklist", "/FO", "CSV", "/NH"], text=True, timeout=5)
                for line in out.splitlines():
                    if name.lower() in line.lower():
                        procs.append({"raw": line[:200]})
            except Exception:
                pass
        else:
            try:
                out = subprocess.check_output(["ps", "-eo", "pid,comm,args"], text=True, timeout=5)
                for line in out.splitlines():
                    if name.lower() in line.lower():
                        procs.append({"raw": line.strip()[:200]})
            except Exception:
                pass
        if not dry_run and procs:
            raise AutomationError("Kill requires explicit tool 'close_application' or PIN-gated power_control; this is a dry-run scanner only.")
        return {"query": name, "matches": procs[:20], "count": len(procs), "dry_run": dry_run}
