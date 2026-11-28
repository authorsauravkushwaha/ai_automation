"""Dev Assistant — git, code review, dependency audit, test runner.

All operations are bounded, local, and never auto-push without explicit consent.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from hinata.automation.app_control import AutomationError
from hinata.security.offline_guard import offline_child_env
from hinata.security.process_output import checked_output

# The assistant runs on Windows as a pythonw GUI where no console is attached.
# Launching git/pytest without a window and without inheriting a stray console
# avoids flashing black windows for every command.
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
_GIT_BASE = ["git", "-c", "core.quotepath=false", "-c", "core.hooksPath=" + os.devnull, "-c", "diff.external=", "-c", "core.fsmonitor=false"]


def _git_env() -> dict[str, str]:
    """Git environment that disables prompting and terminal pagers."""
    env = offline_child_env()
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_PAGER"] = "cat"
    env["PAGER"] = "cat"
    return env


def _git_failure(exc: subprocess.CalledProcessError) -> str:
    """Return git's own explanation, not Python's.

    ``str(CalledProcessError)`` is just "Command '[...]' returned non-zero exit
    status 128", which told the user nothing. git puts the actual reason on
    stderr ("fatal: not a git repository", "nothing to commit", …), so prefer
    that, then stdout, and fall back to the exception text.
    """
    stderr = (getattr(exc, "stderr", "") or "").strip()
    stdout = (getattr(exc, "output", "") or "").strip()
    detail = stderr or stdout
    if detail:
        lines = [line.strip() for line in detail.splitlines() if line.strip()]
        # ``git diff`` prints its whole usage block on a bad invocation, so the
        # last line is a random option. Prefer the actual diagnostic.
        for line in lines:
            if line.startswith(("fatal:", "error:", "warning:")):
                return line[:400]
        return (lines[0] if lines else detail)[:400]
    return str(exc)[:400]


def _python_executable() -> str:
    """Interpreter to run tests with; ``python`` may not exist on Linux/macOS."""
    return sys.executable or ("python" if os.name == "nt" else "python3")


class DevAssistant:
    def __init__(self, files=None, workspace_manager=None) -> None:
        self.files = files
        self.workspaces = workspace_manager

    def git_status(self, workspace: str = "") -> dict[str, Any]:
        root = self._git_root(workspace)
        try:
            # ``git status -b`` always emits a branch header. The tracked change list
            # comes from --porcelain so the summary is accurate.
            status = checked_output(
                _GIT_BASE + ["status", "--porcelain=v1", "-b"], cwd=root, text=True,
                timeout=10, env=_git_env(), stderr=subprocess.STDOUT
            )
            branch = ""
            ahead_behind = ""
            for line in status.splitlines():
                if line.startswith("##"):
                    header = line[3:].strip()
                    # On an unborn branch git says "No commits yet on master";
                    # splitting on the first space reported the branch as "No".
                    if header.startswith("No commits yet on "):
                        branch = header[len("No commits yet on ") :].strip()
                    else:
                        branch = header.split("...")[0].split(" ")[0].strip()
                    if "[" in header and header.endswith("]"):
                        ahead_behind = header[header.index("[") + 1 : -1]
                    break
            changed = [line for line in status.splitlines() if line.strip() and not line.startswith("##")]
            try:
                log = checked_output(
                    _GIT_BASE + ["log", "--oneline", "-5"], cwd=root, text=True,
                    timeout=10, env=_git_env(), stderr=subprocess.DEVNULL,
                )
            except subprocess.CalledProcessError:
                # A freshly ``git init``'d repository has no commits, so ``git
                # log`` exits 128 ("does not have any commits yet"). That is a
                # normal first-day state, not a failure of ``git status``.
                log = ""
            diff_stat = checked_output(_GIT_BASE + ["diff", "--stat"], cwd=root, text=True, timeout=10, env=_git_env())
            return {
                "root": str(root),
                "branch": branch,
                "ahead_behind": ahead_behind,
                "changed_files": len(changed),
                "log": log.strip().splitlines()[:5],
                "diff_stat": diff_stat[:1000],
                "porcelain": status[:2000],
            }
        except subprocess.CalledProcessError as exc:
            raise AutomationError(f"git status failed in {root}: {_git_failure(exc)}") from exc
        except FileNotFoundError as exc:
            raise AutomationError("git is not installed or not on PATH") from exc

    def git_diff(self, workspace: str = "", *, staged: bool = False) -> dict[str, Any]:
        root = self._git_root(workspace)
        args = _GIT_BASE + ["diff", "--no-color", "-U3"]
        if staged:
            args.insert(len(_GIT_BASE) + 1, "--cached")
        try:
            out = checked_output(
                args, cwd=root, text=True, timeout=10, env=_git_env(), stderr=subprocess.STDOUT
            )
            return {"root": str(root), "staged": staged, "diff": out[:10000], "truncated": len(out) > 10000}
        except subprocess.CalledProcessError as exc:
            raise AutomationError(f"git diff failed: {_git_failure(exc)}") from exc
        except FileNotFoundError as exc:
            raise AutomationError("git is not installed or not on PATH") from exc

    def git_commit(self, message: str, workspace: str = "", *, add_all: bool = False) -> dict[str, Any]:
        if not message or len(message.strip()) < 3:
            raise AutomationError("Commit message must be at least 3 chars")
        if len(message) > 500:
            raise AutomationError("Commit message too long (max 500)")
        root = self._git_root(workspace)
        try:
            if add_all:
                checked_output(_GIT_BASE + ["add", "-A"], cwd=root, timeout=10, env=_git_env())
            # Only commit if there's something to commit
            status = checked_output(
                _GIT_BASE + ["status", "--porcelain"], cwd=root, text=True,
                timeout=10, env=_git_env(), stderr=subprocess.STDOUT
            )
            if not status.strip():
                return {"root": str(root), "committed": False, "note": "No changes to commit"}
            out = checked_output(
                _GIT_BASE + ["commit", "-m", message.strip()], cwd=root, text=True,
                timeout=10, env=_git_env(), stderr=subprocess.STDOUT
            )
            rev = checked_output(
                _GIT_BASE + ["rev-parse", "--short", "HEAD"], cwd=root, text=True,
                timeout=10, env=_git_env(), stderr=subprocess.STDOUT
            ).strip()
            return {"root": str(root), "committed": True, "rev": rev, "output": out[:1000]}
        except subprocess.CalledProcessError as exc:
            raise AutomationError(f"git commit failed: {_git_failure(exc)}") from exc
        except FileNotFoundError as exc:
            raise AutomationError("git is not installed or not on PATH") from exc

    def code_review(self, path: str = "", workspace: str = "") -> dict[str, Any]:
        """Heuristic review: long functions, missing docstrings, broad except, hardcoded secrets."""
        root = self._resolve_workspace(workspace) if workspace else (self.files.roots[0] if self.files else Path.cwd())
        target = ((root / path) if path else root).resolve()
        if not self._is_contained(target):
            raise AutomationError("Review target must stay inside approved folders")
        if not target.exists():
            raise AutomationError(f"Path does not exist: {target}")
        findings: list[dict[str, Any]] = []
        py_files: list[Path] = []
        if target.is_file() and target.suffix == ".py":
            py_files = [target]
        elif target.is_dir():
            deadline = time.monotonic() + 5.0
            for visited, (dirpath, dirs, filenames) in enumerate(os.walk(target)):
                dirs[:] = [d for d in dirs if d not in {".git", "__pycache__", ".venv", "node_modules"}]
                if len(py_files) >= 30 or visited >= 1000 or time.monotonic() > deadline:
                    break
                for fn in filenames:
                    if fn.endswith(".py"):
                        py_files.append(Path(dirpath) / fn)
                        if len(py_files) >= 30:
                            break
        for fp in py_files[:30]:
            try:
                text = self._read_contained(fp)
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
        return {"root": str(root), "files_scanned": min(len(py_files), 30), "findings": findings[:100], "total_findings": len(findings)}

    def dependency_audit(self, workspace: str = "") -> dict[str, Any]:
        root = self._resolve_workspace(workspace)
        manifests: list[Path] = []
        for name in ["requirements.txt", "pyproject.toml", "package.json", "Cargo.toml", "go.mod"]:
            p = root / name
            if p.exists():
                manifests.append(p)
        if not manifests:
            return {"kind": "manifest_inventory", "security_scan_performed": False, "root": str(root), "manifests": [], "note": "No dependency manifests found"}
        details: list[dict[str, Any]] = []
        for m in manifests:
            try:
                text = self._read_contained(m)
                lines = [line.strip() for line in text.splitlines() if line.strip() and not line.strip().startswith("#")]
                details.append({"file": str(m.name), "deps": lines[:50], "count": len(lines)})
            except OSError:
                pass
        return {"kind": "manifest_inventory", "security_scan_performed": False, "root": str(root), "manifests": [str(x) for x in manifests], "details": details}

    def run_tests(self, workspace: str = "", target: str = "") -> dict[str, Any]:
        root = self._resolve_workspace(workspace)
        if target and (target.startswith("-") or "\x00" in target):
            raise AutomationError("Test target must be a path, not a command-line option")
        cmd = [_python_executable(), "-m", "pytest", "-q", "--no-header"]
        if target:
            candidate = (root / target).resolve()
            if not candidate.is_relative_to(root):
                raise AutomationError("Test target must stay inside the approved workspace")
            cmd.extend(["--", str(candidate)])
        from hinata.automation.workspace import IsolatedSandboxRunner

        runner = self.workspaces.sandbox if self.workspaces is not None else IsolatedSandboxRunner()
        report = runner.run_command(cmd, cwd=root, timeout_seconds=60)
        return {"root": str(root), "cmd": " ".join(cmd), "returncode": report["exit_code"],
                "stdout": report["stdout"][:3000], "stderr": report["stderr"][:1000],
                "passed": report["exit_code"] == 0, "isolation_backend": report["isolation_backend"]}

    def _read_contained(self, path: Path) -> str:
        resolved = path.resolve()
        if not self._is_contained(resolved):
            raise AutomationError("Read target is outside approved folders")
        if self.files is None:
            raise AutomationError("A guarded file manager is required for developer reads")
        limit = self.files.config.max_read_bytes
        raw = self.files._safe_read_bytes(resolved, limit)
        if len(raw) > limit:
            raise AutomationError("Developer input exceeds configured byte limit")
        return raw.decode("utf-8", errors="replace")

    def _git_root(self, workspace: str) -> Path:
        root = self._resolve_workspace(workspace)
        # Do not let git discover a parent repository outside the approved directory.
        candidate = root
        while not (candidate / ".git").exists():
            if candidate.parent == candidate or not self._is_contained(candidate.parent):
                raise AutomationError("Not a git repository inside approved folders")
            candidate = candidate.parent
        if (candidate / ".git").is_symlink():
            raise AutomationError("Git metadata symlinks are not supported")
        if (candidate / ".git").is_file():
            raise AutomationError("Linked Git worktrees require separately verified metadata; refusing")
        return root

    def _resolve_workspace(self, workspace: str) -> Path:
        """Resolve a workspace strictly inside an approved root.

        This is a security boundary, not a convenience helper. Every git, review,
        audit and test command below runs inside the returned directory, so the
        returned path must never escape the registered workspaces or the permitted
        file roots. A path that the guarded components refuse is a hard failure --
        it must never fall through to an unvalidated ``Path(workspace)``.
        """
        requested = (workspace or "").strip()
        rejections: list[str] = []

        if self.workspaces is not None and requested:
            try:
                root = Path(str(self.workspaces.inventory(requested)["root"])).expanduser().resolve()
            except AutomationError as exc:
                rejections.append(str(exc))
            except Exception as exc:  # unknown workspace id/name: try the next guard
                rejections.append(f"workspace manager: {exc}")
            else:
                if root.is_dir():
                    return root
                rejections.append(f"workspace '{requested}' does not exist")

        if self.files is not None:
            try:
                # FileManager.resolve() re-checks containment after symlink
                # resolution and raises AutomationError when the target escapes.
                return self.files.resolve(requested or ".")
            except AutomationError as exc:
                rejections.append(str(exc))
            except OSError as exc:
                rejections.append(str(exc))

        if requested and self._is_contained(Path(requested)):
            resolved = Path(requested).expanduser().resolve()
            if resolved.is_dir():
                return resolved

        detail = f" ({'; '.join(rejections[:2])})" if rejections else ""
        raise AutomationError(
            f"Workspace '{requested or '.'}' is not inside an approved folder{detail}. "
            "Register it with 'workspaces --register <path>' or move it under a permitted root."
        )

    def _is_contained(self, candidate: Path) -> bool:
        """True only when ``candidate`` resolves inside a permitted root."""
        roots: list[Path] = []
        roots.extend(getattr(self.files, "roots", None) or [])
        roots.extend(getattr(self.workspaces, "allowed_roots", None) or [])
        if not roots:
            return False
        try:
            resolved = candidate.expanduser().resolve()
        except OSError:
            return False
        for root in roots:
            try:
                resolved.relative_to(Path(root).expanduser().resolve())
                return True
            except (ValueError, OSError):
                continue
        return False
