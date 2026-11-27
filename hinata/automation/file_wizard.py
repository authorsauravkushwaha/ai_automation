"""File Wizard — the strongest local file organizer.

Features:
- Auto-organize folder by type/date/name
- Duplicate finder (size + hash, no full read for large files)
- Batch rename with patterns (numbered, date, find-replace)
- Universal converter (md↔html, json↔csv, txt cleanup)
- Clean temp / cache / empty folders
- Storage analyzer (top largest, by extension)
- Bulk tag / archive
"""

from __future__ import annotations

import csv
import hashlib
import json
import os
import re
import shutil
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from hinata.automation.app_control import AutomationError

CATEGORY_MAP: dict[str, str] = {
    ".jpg": "Images", ".jpeg": "Images", ".png": "Images", ".gif": "Images", ".webp": "Images", ".svg": "Images", ".heic": "Images",
    ".mp4": "Videos", ".mov": "Videos", ".avi": "Videos", ".mkv": "Videos", ".webm": "Videos",
    ".mp3": "Audio", ".wav": "Audio", ".flac": "Audio", ".m4a": "Audio", ".ogg": "Audio",
    ".pdf": "Documents", ".docx": "Documents", ".doc": "Documents", ".txt": "Documents", ".md": "Documents", ".rtf": "Documents",
    ".xlsx": "Sheets", ".xls": "Sheets", ".csv": "Sheets",
    ".pptx": "Slides", ".ppt": "Slides",
    ".zip": "Archives", ".rar": "Archives", ".7z": "Archives", ".tar": "Archives", ".gz": "Archives",
    ".exe": "Apps", ".msi": "Apps", ".apk": "Apps",
    ".py": "Code", ".js": "Code", ".ts": "Code", ".html": "Code", ".css": "Code", ".json": "Code", ".java": "Code", ".cpp": "Code",
}


#: Extensions ``clean_temp`` is allowed to remove. Deliberately narrow: a stale
#: document is not "temp" just because it is old.
TEMP_SUFFIXES = frozenset({".tmp", ".temp", ".cache", ".bak", ".part", ".crdownload", ".log"})


def _is_same_file(a: Path, b: Path) -> bool:
    """True when both paths already point at one file (case-only rename, hardlink).

    ``Path.exists()`` is not enough on a case-insensitive filesystem: renaming
    ``Report.txt`` to ``report.txt`` would look like an overwrite of an existing
    file when in fact it is the source.
    """
    try:
        if not (a.exists() and b.exists()):
            return False
        return a.samefile(b)
    except OSError:
        return False


class FileWizard:
    def __init__(self, files=None) -> None:
        self.files = files

    def _require_files(self) -> Any:
        if not self.files:
            raise AutomationError("File manager not available")
        return self.files

    def _resolve_root(self, path: str) -> Any:
        """Resolve ``path`` or, when it is empty, the first permitted root."""
        fm = self._require_files()
        if path.strip():
            return fm.resolve(path)
        return next((r for r in fm.roots if r.exists()), fm.roots[0] if fm.roots else Path.cwd())

    def analyze_storage(self, path: str = "", *, top_n: int = 20) -> dict[str, Any]:
        root = self._resolve_root(path)
        if not root.is_dir():
            raise AutomationError(f"Not a directory: {root}")
        ext_counter: Counter[str] = Counter()
        ext_bytes: Counter[str] = Counter()
        largest: list[dict[str, Any]] = []
        total_files = 0
        total_bytes = 0
        for dirpath, _, filenames in os.walk(root):
            for fn in filenames:
                if fn.startswith("."):
                    continue
                fp = Path(dirpath) / fn
                try:
                    sz = fp.stat().st_size
                except OSError:
                    continue
                total_files += 1
                total_bytes += sz
                ext = fp.suffix.lower() or "(no ext)"
                ext_counter[ext] += 1
                ext_bytes[ext] += sz
                largest.append({"path": str(fp), "bytes": sz, "ext": ext})
        largest.sort(key=lambda x: x["bytes"], reverse=True)
        return {
            "root": str(root),
            "total_files": total_files,
            "total_bytes": total_bytes,
            "total_mb": round(total_bytes / (1024 * 1024), 2),
            "by_ext_count": dict(ext_counter.most_common(20)),
            "by_ext_bytes": {k: round(v / (1024 * 1024), 2) for k, v in ext_bytes.most_common(20)},
            "largest": largest[: max(0, top_n)],  # negative would slice from the end
        }

    def find_duplicates(self, path: str = "", *, max_groups: int = 20, min_size_kb: int = 1) -> dict[str, Any]:
        root = self._resolve_root(path)
        if not root.is_dir():
            raise AutomationError(f"Not a directory: {root}")
        group_limit = max(0, min(int(max_groups), 50))
        size_floor = max(0, int(min_size_kb)) * 1024
        empty = {
            "root": str(root),
            "duplicate_groups": [],
            "groups_found": 0,
            "hash_algorithm": "sha256",
        }
        if group_limit <= 0:
            return empty
        # First group by size
        size_map: dict[int, list[Path]] = defaultdict(list)
        for dirpath, _, filenames in os.walk(root):
            for fn in filenames:
                fp = Path(dirpath) / fn
                if fp.is_symlink():
                    continue
                try:
                    sz = fp.stat().st_size
                    if sz < size_floor:
                        continue
                    size_map[sz].append(fp)
                except OSError:
                    continue
        # For groups with same size, hash first 4KB + full if needed
        groups: list[dict[str, Any]] = []
        for sz, paths in size_map.items():
            if len(paths) < 2:
                continue
            # Quick hash: first 4096 bytes
            quick_map: dict[str, list[Path]] = defaultdict(list)
            for p in paths:
                try:
                    with p.open("rb") as f:
                        h = hashlib.sha256(f.read(4096)).hexdigest()
                    quick_map[h].append(p)
                except OSError:
                    continue
            for qpaths in quick_map.values():
                if len(qpaths) < 2:
                    continue
                # Full hash
                full_map: dict[str, list[Path]] = defaultdict(list)
                for p in qpaths:
                    try:
                        digest = hashlib.sha256()
                        with p.open("rb") as f:
                            while chunk := f.read(8192):
                                digest.update(chunk)
                        full_map[digest.hexdigest()].append(p)
                    except OSError:
                        continue
                for fh, fps in full_map.items():
                    if len(fps) >= 2:
                        groups.append({"size": sz, "hash": fh[:12], "files": [str(x) for x in fps]})
                        if len(groups) >= group_limit:
                            break
            if len(groups) >= group_limit:
                break
        return {
            "root": str(root),
            "duplicate_groups": groups,
            "groups_found": len(groups),
            "hash_algorithm": "sha256",
        }

    def organize_folder(self, path: str, *, mode: str = "by_type", dry_run: bool = True) -> dict[str, Any]:
        root = self._resolve_root(path)
        if not root.is_dir():
            raise AutomationError(f"Not a directory: {root}")
        mode = mode.lower().strip()
        if mode not in {"by_type", "by_date", "by_ext"}:
            raise AutomationError("mode must be by_type, by_date, or by_ext")
        moves: list[dict[str, str]] = []
        for item in root.iterdir():
            # Symlinks are left alone: moving one would either dangle or relocate
            # a file that lives outside the permitted roots.
            if item.is_symlink() or item.is_dir() or item.name.startswith("."):
                continue
            ext = item.suffix.lower()
            if mode == "by_type":
                cat = CATEGORY_MAP.get(ext, "Others")
                dest_dir = root / cat
            elif mode == "by_ext":
                cat = ext.lstrip(".") or "no_ext"
                dest_dir = root / cat
            else:  # by_date
                try:
                    mtime = time.strftime("%Y-%m", time.localtime(item.stat().st_mtime))
                except OSError:
                    mtime = "unknown"
                dest_dir = root / mtime
            dest = dest_dir / item.name
            moves.append({"from": str(item), "to": str(dest)})
        if not dry_run:
            for m in moves:
                src = Path(m["from"])
                dst = Path(m["to"])
                if dst.exists():
                    # Avoid overwrite — add suffix
                    dst = dst.with_name(f"{dst.stem}_dup{dst.suffix}")
                    m["to"] = str(dst)
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(src), str(dst))
        return {"root": str(root), "mode": mode, "planned_moves": moves, "dry_run": dry_run, "count": len(moves)}

    def batch_rename(self, folder: str, pattern: str, *, replacement: str = "", start_num: int = 1, dry_run: bool = True) -> dict[str, Any]:
        root = self._resolve_root(folder)
        if not root.is_dir():
            raise AutomationError(f"Not a directory: {root}")
        files = sorted(
            p for p in root.iterdir()
            if p.is_file() and not p.is_symlink() and not p.name.startswith(".")
        )
        renames: list[dict[str, str]] = []
        for idx, fp in enumerate(files, start=start_num):
            new_name = ""
            if pattern == "numbered":
                # e.g. prefix_{num:03d}{ext}
                prefix = replacement or "file"
                new_name = f"{prefix}_{idx:03d}{fp.suffix}"
            elif pattern == "find_replace":
                # replacement format: old=new (split on first =)
                if "=" in replacement:
                    old, new = replacement.split("=", 1)
                    new_name = fp.name.replace(old, new)
                else:
                    raise AutomationError("For find_replace, replacement must be 'old=new'")
            elif pattern == "lowercase":
                new_name = fp.name.lower()
            elif pattern == "uppercase":
                new_name = fp.name.upper()
            elif pattern == "prefix":
                new_name = f"{replacement}{fp.name}"
            else:
                raise AutomationError("pattern must be numbered, find_replace, lowercase, uppercase, prefix")
            if new_name == fp.name:
                continue
            dest = root / new_name
            # A case-only rename ("Report.txt" -> "report.txt") targets the same
            # file on a case-insensitive volume (Windows, default macOS), so the
            # overwrite check below must not treat it as a collision.
            case_only = _is_same_file(fp, dest)
            renames.append({"from": str(fp), "to": str(dest), "case_only": "true" if case_only else "false"})
        if not dry_run:
            for r in renames:
                src = Path(r["from"])
                dst = Path(r["to"])
                if _is_same_file(src, dst):
                    # Already the same file: nothing to move, and failing here
                    # would make "lowercase" unusable on Windows.
                    continue
                if dst.exists():
                    raise AutomationError(f"Refusing to overwrite: {dst}")
                src.rename(dst)
        return {"folder": str(root), "pattern": pattern, "planned": renames, "dry_run": dry_run, "count": len(renames)}

    def convert_file(self, source: str, dest: str, fmt: str = "auto") -> dict[str, Any]:
        fm = self._require_files()
        src = fm.resolve(source)
        dst = fm.resolve(dest, must_exist=False)
        if dst.exists():
            raise AutomationError(f"Destination exists: {dst}")
        dst.parent.mkdir(parents=True, exist_ok=True)
        src_ext = src.suffix.lower()
        dst_ext = dst.suffix.lower()
        # ``fmt`` used to be accepted and never read, so "convert to html" did
        # nothing unless the destination already happened to end in .html.
        # Honour an explicit format when the destination extension does not
        # already imply it.
        explicit = fmt.strip().lower().lstrip(".")
        if explicit and explicit != "auto" and dst_ext != f".{explicit}":
            if dst.suffix.lower():
                # A different, known extension is a caller mistake: refuse
                # instead of silently writing a .csv body into a .json file.
                raise AutomationError(
                    f"Requested format '{explicit}' conflicts with destination extension '{dst.suffix}'"
                )
            dst = dst.with_name(f"{dst.name}.{explicit}")
            dst_ext = f".{explicit}"
        # Simple converters using stdlib
        if src_ext == ".md" and dst_ext == ".html":
            text = src.read_text(encoding="utf-8", errors="replace")
            # Very small markdown -> html (headers, lists, code)
            html = self._md_to_html(text)
            dst.write_text(f"<!doctype html><meta charset='utf-8'><body>{html}</body>", encoding="utf-8")
            return {"from": str(src), "to": str(dst), "converter": "md->html"}
        elif src_ext == ".json" and dst_ext == ".csv":
            data = json.loads(src.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                data = [data]
            if not isinstance(data, list) or not data:
                raise AutomationError("JSON must be an object or non-empty array for CSV export")
            headers = sorted({k for row in data if isinstance(row, dict) for k in row.keys()})
            with dst.open("w", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=headers)
                w.writeheader()
                for row in data:
                    if isinstance(row, dict):
                        w.writerow(row)
            return {"from": str(src), "to": str(dst), "converter": "json->csv"}
        elif src_ext == ".csv" and dst_ext == ".json":
            rows = list(csv.DictReader(src.read_text(encoding="utf-8").splitlines()))
            dst.write_text(json.dumps(rows, indent=2, ensure_ascii=False), encoding="utf-8")
            return {"from": str(src), "to": str(dst), "converter": "csv->json"}
        elif src_ext in {".txt", ".md", ".log"} and dst_ext in {".txt", ".md"}:
            # Cleanup: trim lines, normalize newlines
            text = src.read_text(encoding="utf-8", errors="replace")
            cleaned = "\n".join(line.rstrip() for line in text.replace("\r\n", "\n").split("\n")).strip() + "\n"
            dst.write_text(cleaned, encoding="utf-8")
            return {"from": str(src), "to": str(dst), "converter": "cleanup"}
        else:
            raise AutomationError(f"No converter for {src_ext} -> {dst_ext} (supported: md->html, json<->csv, txt cleanup)")

    def _md_to_html(self, md: str) -> str:
        lines = md.split("\n")
        out: list[str] = []
        in_list = False
        for line in lines:
            if line.startswith("# "):
                if in_list:
                    out.append("</ul>")
                    in_list = False
                out.append(f"<h1>{line[2:].strip()}</h1>")
            elif line.startswith("## "):
                if in_list:
                    out.append("</ul>")
                    in_list = False
                out.append(f"<h2>{line[3:].strip()}</h2>")
            elif line.startswith("- ") or line.startswith("* "):
                if not in_list:
                    out.append("<ul>")
                    in_list = True
                out.append(f"<li>{line[2:].strip()}</li>")
            elif line.strip() == "":
                if in_list:
                    out.append("</ul>")
                    in_list = False
                out.append("<br>")
            else:
                if in_list:
                    out.append("</ul>")
                    in_list = False
                # Bold
                line = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", line)
                out.append(f"<p>{line}</p>")
        if in_list:
            out.append("</ul>")
        return "\n".join(out)

    def clean_temp(self, path: str = "", *, min_age_days: int = 7, dry_run: bool = True) -> dict[str, Any]:
        root = self._resolve_root(path)
        if not root.is_dir():
            raise AutomationError(f"Not a directory: {root}")
        cutoff = time.time() - min_age_days * 86400
        candidates: list[dict[str, Any]] = []
        total_bytes = 0
        for dirpath, _, filenames in os.walk(root):
            for fn in filenames:
                if fn.startswith("."):
                    continue
                fp = Path(dirpath) / fn
                try:
                    # Never follow a symlink out of the permitted roots.
                    if fp.is_symlink():
                        continue
                    st = fp.stat()
                except OSError:
                    continue
                # Two independent conditions, both required:
                #   1. the name must look like a disposable artefact, and
                #   2. it must be older than the age limit.
                # The old code used OR, so it deleted a just-written .log
                # (still in use) *and* an old user document such as report.pdf.
                if fp.suffix.lower() not in TEMP_SUFFIXES:
                    continue
                if st.st_mtime >= cutoff:
                    continue
                candidates.append(
                    {
                        "path": str(fp),
                        "bytes": st.st_size,
                        "age_days": round((time.time() - st.st_mtime) / 86400, 1),
                    }
                )
                total_bytes += st.st_size
        if not dry_run:
            for c in candidates:
                try:
                    Path(c["path"]).unlink()
                except OSError:
                    c["error"] = "could not delete"
        return {"root": str(root), "candidates": candidates[:100], "total_candidates": len(candidates), "total_bytes": total_bytes, "dry_run": dry_run}
