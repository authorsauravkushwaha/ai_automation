"""Dev Assistant — git, code review, dependency audit, test runner.

All operations are bounded, local, and never auto-push without explicit consent.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any

from hinata.automation.app_control import AutomationError


class DevAssistant:
    def __init__(self, files=None, workspace_manager=None) -> None:
        self.files = files
        self.workspaces = workspace_manager

    def git_status(self, workspace: str = "") -> dict[str, Any]:
        root = self._resolve_workspace(workspace)
        try:
            status = subprocess.check_output(["git", "status", "--porcelain=v1", "-b"], cwd=root, text=True, timeout=10)
            branch = ""
            ahead_behind = ""
            for line in status.splitlines():
                if line.startswith("##"):
                    branch = line[3:].strip()
                    break
            log = subprocess.check_output(["git", "log", "--oneline", "-5"], cwd=root, text=True, timeout=10)
            diff_stat = subprocess.check_output(["git", "diff", "--stat"], cwd=root, text=True, timeout=10)
            return {"root": str(root), "branch": branch, "log": log.strip().splitlines()[:5], "diff_stat": diff_stat[:1000], "porcelain": status[:2000]}
        except (subprocess.CalledProcessError, FileNotFoundError) as exc:
            raise AutomationError(f"git status failed in {root}: {exc}") from exc

    def git_diff(self, workspace: str = "", *, staged: bool = False) -> dict[str, Any]:
        root = self._resolve_workspace(workspace)
        args = ["git", "diff", "--no-color", "-U3"]
        if staged:
            args.insert(2, "--cached")
        try:
            out = subprocess.check_output(args, cwd=root, text=True, timeout=10)
            return {"root": str(root), "staged": staged, "diff": out[:10000], "truncated": len(out) > 10000}
        except Exception as exc:
            raise AutomationError(f"git diff failed: {exc}") from exc

    def git_commit(self, message: str, workspace: str = "", *, add_all: bool = False) -> dict[str, Any]:
        if not message or len(message.strip()) < 3:
            raise AutomationError("Commit message must be at least 3 chars")
        if len(message) > 500:
            raise AutomationError("Commit message too long (max 500)")
        root = self._resolve_workspace(workspace)
        try:
            if add_all:
                subprocess.check_output(["git", "add", "-A"], cwd=root, timeout=10)
            # Only commit if there's something to commit
            status = subprocess.check_output(["git", "status", "--porcelain"], cwd=root, text=True, timeout=10)
            if not status.strip():
                return {"root": str(root), "committed": False, "note": "No changes to commit"}
            out = subprocess.check_output(["git", "commit", "-m", message.strip()], cwd=root, text=True, timeout=10)
            rev = subprocess.check_output(["git", "rev-parse", "--short", "HEAD"], cwd=root, text=True, timeout=10).strip()
            return {"root": str(root), "committed": True, "rev": rev, "output": out[:1000]}
        except subprocess.CalledProcessError as exc:
            raise AutomationError(f"git commit failed: {exc.output if hasattr(exc, 'output') else exc}") from exc

    def code_review(self, path: str = "", workspace: str = "") -> dict[str, Any]:
        """Heuristic review: long functions, missing docstrings, broad except, hardcoded secrets."""
        root = self._resolve_workspace(workspace) if workspace else (self.files.roots[0] if self.files else Path.cwd())
        target = (root / path) if path else root
        if not target.exists():
            raise AutomationError(f"Path does not exist: {target}")
        findings: list[dict[str, Any]] = []
        py_files: list[Path] = []
        if target.is_file() and target.suffix == ".py":
            py_files = [target]
        elif target.is_dir():
            for dirpath, _, filenames in os.walk(target):
                if ".git" in dirpath or "__pycache__" in dirpath:
                    continue
                for fn in filenames:
                    if fn.endswith(".py"):
                        py_files.append(Path(dirpath) / fn)
                        if len(py_files) >= 30:
                            break
        for fp in py_files[:30]:
            try:
                text = fp.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            lines = text.splitlines()
            for idx, line in enumerate(lines, 1):
                if len(line) > 120:
                    findings.append({"file": str(fp.relative_to(root) if fp.is_relative_to(root) else fp), "line": idx, "kind": "long_line", "msg": f"Line {idx} is {len(line)} chars (limit 120)"})
                if "except:" in line and "except Exception" not in line:
                    findings.append({"file": str(fp.name), "line": idx, "kind": "bare_except", "msg": "Bare except: without exception type"})
                if re.search(r"password\s*=\s*[\"']", line, re.I) or re.search(r"api_key\s*=\s*[\"']", line, re.I):
                    findings.append({"file": str(fp.name), "line": idx, "kind": "hardcoded_secret", "msg": "Possible hardcoded secret"})
            # Check for missing docstring in top-level functions
            for m in re.finditer(r"^def\s+(\w+)\s*\(", text, re.M):
                # Look ahead for docstring
                start = m.end()
                snippet = text[start:start+200]
                if '"""' not in snippet and "'''" not in snippet:
                    findings.append({"file": str(fp.name), "line": text[:m.start()].count("\n")+1, "kind": "missing_docstring", "msg": f"Function {m.group(1)} may lack docstring"})
        return {"root": str(root), "files_scanned": len(py_files), "findings": findings[:100], "total_findings": len(findings)}

    def dependency_audit(self, workspace: str = "") -> dict[str, Any]:
        root = self._resolve_workspace(workspace)
        manifests: list[Path] = []
        for name in ["requirements.txt", "pyproject.toml", "package.json", "Cargo.toml", "go.mod"]:
            p = root / name
            if p.exists():
                manifests.append(p)
        if not manifests:
            return {"root": str(root), "manifests": [], "note": "No dependency manifests found"}
        details: list[dict[str, Any]] = []
        for m in manifests:
            try:
                text = m.read_text(encoding="utf-8", errors="replace")
                lines = [l.strip() for l in text.splitlines() if l.strip() and not l.strip().startswith("#")]
                details.append({"file": str(m.name), "deps": lines[:50], "count": len(lines)})
            except OSError:
                pass
        return {"root": str(root), "manifests": [str(x) for x in manifests], "details": details}

    def run_tests(self, workspace: str = "", target: str = "") -> dict[str, Any]:
        root = self._resolve_workspace(workspace)
        cmd = ["python", "-m", "pytest", "-q"]
        if target:
            cmd.append(target)
        try:
            proc = subprocess.run(cmd, cwd=root, capture_output=True, text=True, timeout=60)
            return {"root": str(root), "cmd": " ".join(cmd), "returncode": proc.returncode, "stdout": proc.stdout[:3000], "stderr": proc.stderr[:1000], "passed": proc.returncode == 0}
        except FileNotFoundError:
            raise AutomationError("pytest not found; install with pip install pytest") from None
        except subprocess.TimeoutExpired:
            raise AutomationError("Tests timed out after 60s") from None

    def _resolve_workspace(self, workspace: str) -> Path:
        if self.workspaces and workspace:
            # Try workspace manager
            try:
                inv = self.workspaces.inventory(workspace)
                return Path(inv["root"])
            except Exception:
                pass
        if self.files:
            # Try file manager resolve
            try:
                return self.files.resolve(workspace or ".")
            except Exception:
                pass
        # Fallback: cwd or explicit path
        if workspace and Path(workspace).exists():
            return Path(workspace).resolve()
        return Path.cwd().resolve()
