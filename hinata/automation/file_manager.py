"""Bounded file operations restricted to configured roots."""

from __future__ import annotations

import errno
import os
import shutil
import stat
import time
import zipfile
from pathlib import Path
from typing import Any

from hinata.automation.app_control import AutomationError
from hinata.config import SecurityConfig

BLOCKED_OPEN_SUFFIXES = {
    ".exe", ".com", ".bat", ".cmd", ".ps1", ".msi", ".msp", ".scr",
    ".vbs", ".vbe", ".js", ".jse", ".wsf", ".wsh", ".jar", ".lnk",
}


class FileManager:
    def __init__(self, config: SecurityConfig) -> None:
        self.config = config
        # Canonicalize here as well as in Config: direct embedding/tests must get the
        # same short-path, symlink, and traversal protection as the normal loader.
        self.roots = [path.expanduser().resolve() for path in config.allowed_roots]

    def _inside_root(self, path: Path, root: Path) -> bool:
        try:
            path.relative_to(root)
            return True
        except ValueError:
            return False

    def resolve(self, value: str, *, must_exist: bool = True) -> Path:
        if not value.strip():
            raise AutomationError("A file or folder path is required")
        supplied = Path(value.strip().strip('"')).expanduser()
        candidates = [supplied] if supplied.is_absolute() else [root / supplied for root in self.roots]
        if not supplied.is_absolute():
            for root in self.roots:
                parts = supplied.parts
                if parts and parts[0].casefold() == root.name.casefold():
                    # Accept both "note.txt" and "Documents/note.txt" for the Documents root.
                    candidates.insert(0, root.joinpath(*parts[1:]))
        fallback = candidates[0]
        for candidate in candidates:
            try:
                resolved = candidate.resolve(strict=False)
            except OSError as exc:
                raise AutomationError(f"Cannot resolve path: {exc}") from exc
            if not any(self._inside_root(resolved, root) for root in self.roots):
                continue
            if not must_exist or resolved.exists():
                return resolved
        resolved_fallback = fallback.resolve(strict=False)
        if not any(self._inside_root(resolved_fallback, root) for root in self.roots):
            raise AutomationError("Access denied: the path is outside all permitted folders")
        if must_exist:
            raise AutomationError(f"Path does not exist inside permitted folders: {value}")
        return resolved_fallback

    def open_folder(self, value: str) -> dict[str, Any]:
        path = self.resolve(value)
        if not path.is_dir():
            raise AutomationError(f"Not a folder: {path}")
        self._open_with_windows(path)
        return {"path": str(path)}

    def open_file(self, value: str) -> dict[str, Any]:
        path = self.resolve(value)
        if not path.is_file():
            raise AutomationError(f"Not a file: {path}")
        if path.suffix.lower() in BLOCKED_OPEN_SUFFIXES:
            raise AutomationError("Executable, shortcut, and script files cannot be opened by HINATA")
        self._open_with_windows(path)
        return {"path": str(path)}

    @staticmethod
    def _open_with_windows(path: Path) -> None:
        if os.name != "nt":
            raise AutomationError("Opening files with desktop applications is supported only on Windows 11")
        try:
            os.startfile(str(path))  # type: ignore[attr-defined]
        except OSError as exc:
            raise AutomationError(f"Windows could not open {path}: {exc}") from exc

    def create_folder(self, value: str) -> dict[str, Any]:
        path = self.resolve(value, must_exist=False)
        try:
            path.mkdir(parents=True, exist_ok=False)
        except FileExistsError as exc:
            raise AutomationError(f"Folder already exists: {path}") from exc
        except OSError as exc:
            raise AutomationError(f"Could not create folder: {exc}") from exc
        return {"path": str(path)}

    def _safe_read_bytes(self, path: Path, limit: int) -> bytes:
        """Open file with O_NOFOLLOW when available and verify inode/realpath post-open."""
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            pre_lstat = path.lstat()
        except OSError as exc:
            raise AutomationError(f"Could not stat {path}: {exc}") from exc
        if stat.S_ISLNK(pre_lstat.st_mode):
            raise AutomationError(f"Refusing to open symbolic link directly: {path}")
        try:
            fd = os.open(path, flags)
        except OSError as exc:
            if exc.errno == getattr(errno, "ELOOP", None):
                raise AutomationError(f"Symbolic link swap detected while opening {path}") from exc
            raise AutomationError(f"Could not open {path}: {exc}") from exc
        try:
            post_stat = os.fstat(fd)
            if not stat.S_ISREG(post_stat.st_mode):
                raise AutomationError(f"Not a regular file: {path}")
            if (pre_lstat.st_ino and post_stat.st_ino and pre_lstat.st_ino != post_stat.st_ino) or (
                pre_lstat.st_dev and post_stat.st_dev and pre_lstat.st_dev != post_stat.st_dev
            ):
                raise AutomationError(f"File identity changed between check and open: {path}")
            re_resolved = path.resolve(strict=True)
            if not any(self._inside_root(re_resolved, root) for root in self.roots):
                raise AutomationError("Access denied: file escaped permitted root after open")
            chunks: list[bytes] = []
            remaining = limit + 1
            while remaining > 0:
                piece = os.read(fd, min(65536, remaining))
                if not piece:
                    break
                chunks.append(piece)
                remaining -= len(piece)
            return b"".join(chunks)
        finally:
            os.close(fd)

    def read_text(self, value: str, max_chars: int = 12000) -> dict[str, Any]:
        path = self.resolve(value)
        if not path.is_file():
            raise AutomationError(f"Not a file: {path}")
        if path.suffix.lower() not in self.config.text_extensions:
            raise AutomationError(f"Files with extension '{path.suffix}' are not approved for text reading")
        # Clamp like ``list_directory`` does: a negative bound would slice from
        # the end (``decoded[:-1]`` returned everything but the last character)
        # while still reporting ``truncated=True``, i.e. the opposite of the
        # caller's intent.
        max_chars = max(0, min(int(max_chars), self.config.max_read_bytes))
        raw = self._safe_read_bytes(path, self.config.max_read_bytes)
        truncated = len(raw) > self.config.max_read_bytes
        decoded = raw[: self.config.max_read_bytes].decode("utf-8-sig", errors="replace")
        truncated = truncated or len(decoded) > max_chars
        return {"path": str(path), "content": decoded[:max_chars], "truncated": truncated}

    def list_directory(self, value: str = "", max_entries: int = 100) -> dict[str, Any]:
        base = self.resolve(value) if value.strip() else self.roots[0]
        if not base.is_dir():
            raise AutomationError(f"Not a folder: {base}")
        entries: list[dict[str, Any]] = []
        truncated = False
        try:
            for item in sorted(base.iterdir(), key=lambda p: p.name.casefold()):
                if item.name.startswith("."):
                    continue
                if len(entries) >= max(1, min(max_entries, 200)):
                    truncated = True
                    break
                is_link = item.is_symlink()
                resolved = item.resolve(strict=False)
                if not any(self._inside_root(resolved, root) for root in self.roots):
                    continue
                kind = "symlink" if is_link else ("dir" if item.is_dir() else "file")
                size = item.stat().st_size if kind == "file" else 0
                entries.append({"name": item.name, "path": str(resolved), "kind": kind, "size": size})
        except OSError as exc:
            raise AutomationError(f"Could not list directory {base}: {exc}") from exc
        return {"path": str(base), "entries": entries, "truncated": truncated}

    def search_content(self, query: str, root: str = "", max_results: int = 20) -> dict[str, Any]:
        """Bounded full-text content search across approved text extensions inside permitted roots."""
        if not query.strip():
            raise AutomationError("A non-empty content search query is required")
        roots = [self.resolve(root)] if root.strip() else list(self.roots)
        deadline = time.monotonic() + self.config.search_timeout_seconds
        needle = query.casefold()
        matches: list[dict[str, Any]] = []
        visited = 0
        timed_out = False
        for base in roots:
            if not base.exists() or not base.is_dir():
                continue
            for directory, dirnames, filenames in os.walk(base, followlinks=False):
                visited += 1
                if visited > self.config.search_max_directories or time.monotonic() >= deadline:
                    timed_out = True
                    break
                dirnames[:] = sorted(
                    name
                    for name in dirnames
                    if not name.startswith(".")
                    and name not in {"$RECYCLE.BIN", "System Volume Information", "node_modules", "__pycache__"}
                )
                for filename in sorted(filenames):
                    fpath = Path(directory) / filename
                    if fpath.is_symlink() or fpath.suffix.lower() not in self.config.text_extensions:
                        continue
                    resolved = fpath.resolve(strict=False)
                    if not any(self._inside_root(resolved, allowed) for allowed in self.roots):
                        continue
                    try:
                        if resolved.stat().st_size > self.config.max_read_bytes:
                            continue
                        raw = self._safe_read_bytes(resolved, min(self.config.max_read_bytes, 250_000))
                        text = raw.decode("utf-8-sig", errors="replace")
                    except (AutomationError, OSError):
                        continue
                    for line_no, line in enumerate(text.splitlines(), 1):
                        if needle in line.casefold():
                            matches.append(
                                {
                                    "path": str(resolved),
                                    "line": line_no,
                                    "snippet": line.strip()[:240],
                                }
                            )
                            if len(matches) >= max(1, min(max_results, 50)):
                                return {"matches": matches, "truncated": True, "directories_searched": visited}
                            break
                    if time.monotonic() >= deadline:
                        timed_out = True
                        break
                if timed_out:
                    break
            if timed_out:
                break
        return {"matches": matches, "truncated": timed_out, "directories_searched": visited}

    def write_text_file(self, value: str, content: str, *, overwrite: bool = False) -> dict[str, Any]:
        path = self.resolve(value, must_exist=False)
        if path.suffix.lower() not in self.config.text_extensions:
            raise AutomationError(f"Extension '{path.suffix}' is not approved for text writing")
        if path.name.lower().startswith(".env") or path.suffix.lower() in {".pem", ".key", ".pfx"}:
            raise AutomationError("Refusing to write credential or environment files")
        encoded = content.encode("utf-8")
        if len(encoded) > self.config.max_read_bytes:
            raise AutomationError("Content exceeds configured byte limit")
        if path.exists() and not overwrite:
            raise AutomationError(f"File already exists (overwrite=False): {path}")
        if path.is_symlink():
            raise AutomationError("Refusing to write through a symbolic link")
        path.parent.mkdir(parents=True, exist_ok=True)
        parent_resolved = path.parent.resolve(strict=True)
        if not any(self._inside_root(parent_resolved, root) for root in self.roots):
            raise AutomationError("Access denied: parent directory escaped permitted roots")
        temp = path.with_name(f".{path.name}.hinata-tmp-{os.getpid()}")
        try:
            temp.write_bytes(encoded)
            temp.replace(path)
        except OSError as exc:
            try:
                temp.unlink(missing_ok=True)
            except OSError:
                pass
            raise AutomationError(f"Could not write {path}: {exc}") from exc
        return {"path": str(path), "bytes_written": len(encoded)}

    def copy_file(self, source: str, destination: str) -> dict[str, Any]:
        src = self.resolve(source)
        dst = self.resolve(destination, must_exist=False)
        if not src.is_file() or src.is_symlink():
            raise AutomationError(f"Source must be a regular file: {src}")
        if dst.exists():
            raise AutomationError(f"Destination already exists: {dst}")
        if dst.suffix.lower() in BLOCKED_OPEN_SUFFIXES:
            raise AutomationError("Refusing to create executable or script files via copy")
        dst.parent.mkdir(parents=True, exist_ok=True)
        data = self._safe_read_bytes(src, self.config.max_read_bytes)
        if len(data) > self.config.max_read_bytes:
            raise AutomationError("Source file exceeds configured size limit for safe copy")
        dst.write_bytes(data)
        return {"source": str(src), "destination": str(dst), "bytes": len(data)}

    def move_or_rename(self, source: str, destination: str) -> dict[str, Any]:
        src = self.resolve(source)
        dst = self.resolve(destination, must_exist=False)
        if src.is_symlink() or dst.exists():
            raise AutomationError("Cannot move/rename symlink or overwrite existing destination")
        if dst.suffix.lower() in BLOCKED_OPEN_SUFFIXES:
            raise AutomationError("Refusing to rename into an executable extension")
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))
        return {"source": str(src), "destination": str(dst)}

    def compare_files(self, first: str, second: str) -> dict[str, Any]:
        import difflib

        a = self.read_text(first, 20000)
        b = self.read_text(second, 20000)
        diff_lines = list(
            difflib.unified_diff(
                a["content"].splitlines(),
                b["content"].splitlines(),
                fromfile=Path(a["path"]).name,
                tofile=Path(b["path"]).name,
                lineterm="",
            )
        )
        return {
            "first": a["path"],
            "second": b["path"],
            "identical": a["content"] == b["content"] and not a["truncated"] and not b["truncated"],
            "diff": "\n".join(diff_lines[:200]),
            "truncated": len(diff_lines) > 200 or a["truncated"] or b["truncated"],
        }

    def create_archive(self, source: str, archive_path: str) -> dict[str, Any]:
        src = self.resolve(source)
        dst = self.resolve(archive_path, must_exist=False)
        if dst.suffix.lower() != ".zip":
            raise AutomationError("Archives must use the .zip extension")
        if dst.exists():
            raise AutomationError(f"Archive already exists: {dst}")
        dst.parent.mkdir(parents=True, exist_ok=True)
        count = 0
        total_bytes = 0
        with zipfile.ZipFile(dst, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            if src.is_file():
                raw = self._safe_read_bytes(src, self.config.max_read_bytes)
                zf.writestr(src.name, raw)
                count = 1
                total_bytes = len(raw)
            elif src.is_dir():
                for dirpath, dirnames, filenames in os.walk(src, followlinks=False):
                    dirnames[:] = [d for d in sorted(dirnames) if not d.startswith(".")]
                    for fname in sorted(filenames):
                        item = Path(dirpath) / fname
                        if item.is_symlink():
                            continue
                        resolved = item.resolve(strict=False)
                        if not self._inside_root(resolved, src):
                            continue
                        size = resolved.stat().st_size
                        if total_bytes + size > self.config.max_read_bytes * 5 or count >= 200:
                            raise AutomationError("Archive exceeds safe file count or byte budget")
                        rel = resolved.relative_to(src).as_posix()
                        zf.write(resolved, arcname=rel)
                        count += 1
                        total_bytes += size
            else:
                raise AutomationError(f"Unsupported archive source: {src}")
        return {"archive": str(dst), "files_archived": count, "total_bytes": total_bytes}

    def quarantine_delete(self, value: str, quarantine_dir: Path) -> dict[str, Any]:
        """Reversible high-risk delete: moves file into private local quarantine rather than permanent unlink."""
        target = self.resolve(value)
        if not target.is_file() or target.is_symlink():
            raise AutomationError("Safe quarantine delete supports regular files only")
        quarantine_dir.mkdir(parents=True, exist_ok=True)
        stamp = int(time.time() * 1000)
        dest = quarantine_dir / f"{stamp}_{target.name}"
        shutil.move(str(target), str(dest))
        return {"deleted_from": str(target), "quarantined_to": str(dest), "reversible": True}

    def search(self, query: str, root: str = "", max_results: int = 20) -> dict[str, Any]:
        # The limit is checked *after* appending, so a non-positive value used
        # to return one match instead of none.
        if int(max_results) <= 0:
            return {"matches": [], "truncated": False, "directories_searched": 0}
        roots = [self.resolve(root)] if root.strip() else list(self.roots)
        deadline = time.monotonic() + self.config.search_timeout_seconds
        needle = query.casefold()
        matches: list[str] = []
        visited = 0
        timed_out = False
        for base in roots:
            if not base.exists() or not base.is_dir():
                continue
            for directory, dirnames, filenames in os.walk(base, followlinks=False):
                visited += 1
                if visited > self.config.search_max_directories or time.monotonic() >= deadline:
                    timed_out = True
                    break
                dirnames[:] = sorted(
                    name for name in dirnames if not name.startswith(".") and name not in {"$RECYCLE.BIN", "System Volume Information"}
                )
                for filename in sorted(filenames):
                    if needle in filename.casefold():
                        candidate = (Path(directory) / filename).resolve(strict=False)
                        if any(self._inside_root(candidate, allowed) for allowed in self.roots):
                            matches.append(str(candidate))
                            if len(matches) >= max_results:
                                return {"matches": matches, "truncated": True, "directories_searched": visited}
                if time.monotonic() >= deadline:
                    timed_out = True
                    break
            if timed_out or len(matches) >= max_results:
                break
        return {"matches": matches, "truncated": timed_out, "directories_searched": visited}
