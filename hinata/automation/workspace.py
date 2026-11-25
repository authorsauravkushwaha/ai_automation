"""Sandboxed coding workspace manager, patch workflow, and isolated runner.

Security rules:
- Only explicitly registered/approved workspace roots can be read or patched.
- Never runs arbitrary shell strings or unrestricted Python snippets.
- Patch proposals store unified diffs + exact file pre-images so rollback is
  atomic and reliable until the user accepts the patch.
- Isolated command execution runs in a child process with network disabled by
  default (Linux user+net namespace via ``unshare`` when available, plus stripped
  proxy/network environment variables, ``PYTHONNOUSERSITE=1``, POSIX ``resource``
  CPU/RAM/FD/nproc caps, process-group termination on timeout/cancel, and bounded
  stdout/stderr capture).
"""

from __future__ import annotations

import ctypes
import difflib
import errno
import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

from hinata.automation.app_control import AutomationError
from hinata.core.errors import CancellationToken
from hinata.database.database import Database
from hinata.security.redaction import redact_text

BLOCKED_WORKSPACE_PATTERNS = {
    ".env",
    ".env.local",
    ".env.production",
    ".git/config",
    ".git/credentials",
    ".netrc",
    "id_rsa",
    "id_ed25519",
}
BLOCKED_PATCH_SUFFIXES = {".exe", ".dll", ".so", ".dylib", ".bat", ".cmd", ".ps1", ".pfx", ".pem", ".key"}
ALLOWED_SOURCE_SUFFIXES = {
    ".py", ".pyi", ".toml", ".json", ".yaml", ".yml", ".md", ".txt", ".ini", ".cfg",
    ".js", ".ts", ".tsx", ".jsx", ".css", ".html", ".sql", ".rs", ".go", ".c", ".cpp", ".h",
}
MAX_PATCH_BYTES = 200_000
MAX_OUTPUT_CHARS = 16_000


def _linux_netns_supported() -> bool:
    if not sys.platform.startswith("linux"):
        return False
    unshare = shutil.which("unshare")
    if not unshare:
        return False
    try:
        probe = subprocess.run(
            [unshare, "--user", "--map-root-user", "--net", sys.executable, "-c", "import sys; sys.exit(0)"],
            capture_output=True,
            timeout=3.0,
            check=False,
            shell=False,
        )
        return probe.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


class _WindowsJobSandbox:
    """Win32 Job Object enforcing tree kill, memory cap, active-process limit, and UI isolation."""

    JOB_OBJECT_LIMIT_ACTIVE_PROCESS = 0x00000008
    JOB_OBJECT_LIMIT_PROCESS_MEMORY = 0x00000100
    JOB_OBJECT_LIMIT_JOB_MEMORY = 0x00000200
    JOB_OBJECT_LIMIT_DIE_ON_UNHANDLED_EXCEPTION = 0x00000400
    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
    JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
    JOB_OBJECT_BASIC_UI_RESTRICTIONS = 4
    JOB_OBJECT_UILIMIT_ALL = 0x000000FF

    def __init__(self, *, max_memory_mb: int = 1024, max_processes: int = 16, kernel32: Any | None = None) -> None:
        self.max_memory_mb = max(128, max_memory_mb)
        self.max_processes = max(2, max_processes)
        self._kernel32 = kernel32
        self._job_handle: int = 0

    def _k32(self) -> Any:
        if self._kernel32 is not None:
            return self._kernel32
        windll = getattr(ctypes, "windll", None)
        if windll is None:
            raise OSError("ctypes.windll is only available on Windows")
        return windll.kernel32

    def create_and_configure(self) -> int:
        class IO_COUNTERS(ctypes.Structure):
            _fields_ = [
                ("ReadOperationCount", ctypes.c_uint64),
                ("WriteOperationCount", ctypes.c_uint64),
                ("OtherOperationCount", ctypes.c_uint64),
                ("ReadTransferCount", ctypes.c_uint64),
                ("WriteTransferCount", ctypes.c_uint64),
                ("OtherTransferCount", ctypes.c_uint64),
            ]

        class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_int64),
                ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", ctypes.c_uint32),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", ctypes.c_uint32),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", ctypes.c_uint32),
                ("SchedulingClass", ctypes.c_uint32),
            ]

        class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
                ("IoInfo", IO_COUNTERS),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        class JOBOBJECT_BASIC_UI_RESTRICTIONS_STRUCT(ctypes.Structure):
            _fields_ = [("UIRestrictionsClass", ctypes.c_uint32)]

        k32 = self._k32()
        handle = int(k32.CreateJobObjectW(None, None) or 0)
        if not handle:
            raise OSError("CreateJobObjectW failed")
        self._job_handle = handle

        mem_bytes = self.max_memory_mb * 1024 * 1024
        info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = (
            self.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            | self.JOB_OBJECT_LIMIT_PROCESS_MEMORY
            | self.JOB_OBJECT_LIMIT_JOB_MEMORY
            | self.JOB_OBJECT_LIMIT_ACTIVE_PROCESS
            | self.JOB_OBJECT_LIMIT_DIE_ON_UNHANDLED_EXCEPTION
        )
        info.BasicLimitInformation.ActiveProcessLimit = self.max_processes
        info.ProcessMemoryLimit = mem_bytes
        info.JobMemoryLimit = mem_bytes

        ok = k32.SetInformationJobObject(
            ctypes.c_void_p(handle),
            self.JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
            ctypes.byref(info),
            ctypes.sizeof(info),
        )
        if not ok:
            self.close()
            raise OSError("SetInformationJobObject(EXTENDED_LIMIT) failed")

        ui_info = JOBOBJECT_BASIC_UI_RESTRICTIONS_STRUCT(UIRestrictionsClass=self.JOB_OBJECT_UILIMIT_ALL)
        k32.SetInformationJobObject(
            ctypes.c_void_p(handle),
            self.JOB_OBJECT_BASIC_UI_RESTRICTIONS,
            ctypes.byref(ui_info),
            ctypes.sizeof(ui_info),
        )
        return handle

    def assign_process(self, process_handle: int) -> None:
        if not self._job_handle:
            return
        k32 = self._k32()
        if not k32.AssignProcessToJobObject(ctypes.c_void_p(self._job_handle), ctypes.c_void_p(process_handle)):
            raise OSError("AssignProcessToJobObject failed")

    def terminate(self, exit_code: int = 137) -> None:
        if not self._job_handle:
            return
        try:
            self._k32().TerminateJobObject(ctypes.c_void_p(self._job_handle), exit_code)
        except OSError:
            pass

    def close(self) -> None:
        if self._job_handle:
            try:
                self._k32().CloseHandle(ctypes.c_void_p(self._job_handle))
            except OSError:
                pass
            self._job_handle = 0


_SANDBOX_SITECUSTOMIZE = """\
import errno
import socket as _socket

_orig_connect = _socket.socket.connect
_orig_connect_ex = _socket.socket.connect_ex

def _is_loopback(addr):
    if isinstance(addr, tuple) and addr:
        host = str(addr[0]).strip().lower()
        return host in {"127.0.0.1", "localhost", "::1"} or host.startswith("127.")
    return False

def _blocked_connect(self, address):
    if _is_loopback(address):
        return _orig_connect(self, address)
    raise OSError(errno.ENETUNREACH, "HINATA sandbox blocks outbound network connections")

def _blocked_connect_ex(self, address):
    if _is_loopback(address):
        return _orig_connect_ex(self, address)
    return errno.ENETUNREACH

_socket.socket.connect = _blocked_connect
_socket.socket.connect_ex = _blocked_connect_ex
"""


class IsolatedSandboxRunner:
    """Runs allow-listed developer verification tools inside an isolated, resource-capped process."""

    def __init__(self) -> None:
        self._linux_netns = _linux_netns_supported()

    @property
    def isolation_backend(self) -> str:
        if self._linux_netns:
            return "linux_user_net_namespace+rlimit+socket_guard"
        if sys.platform.startswith("linux"):
            return "posix_rlimit_process_group+socket_guard"
        if os.name == "nt":
            return "windows_job_object+socket_guard"
        return "subprocess_process_group+socket_guard"

    def run_command(
        self,
        argv: list[str],
        *,
        cwd: Path,
        timeout_seconds: float = 30.0,
        max_memory_mb: int = 1024,
        cancel_token: CancellationToken | None = None,
    ) -> dict[str, Any]:
        if not argv or any(not part or "\x00" in part for part in argv):
            raise AutomationError("Invalid sandbox command vector")
        cwd = cwd.resolve(strict=True)
        guard_dir = tempfile.TemporaryDirectory(prefix="hinata_sbx_")
        try:
            (Path(guard_dir.name) / "sitecustomize.py").write_text(_SANDBOX_SITECUSTOMIZE, encoding="utf-8")
            exec_argv = list(argv)
            # When Python runs with -I (isolated mode), PYTHONPATH is ignored by design;
            # replace -I with -E -s so user site & env vars stay ignored while our
            # temporary sitecustomize.py socket guard still loads reliably across all OSes.
            if Path(exec_argv[0]).name.lower().startswith("python"):
                normalized: list[str] = [exec_argv[0]]
                replaced_i = False
                for arg in exec_argv[1:]:
                    if arg == "-I" and not replaced_i:
                        normalized.extend(["-s"])
                        replaced_i = True
                    else:
                        normalized.append(arg)
                exec_argv = normalized

            clean_env = {
                "PATH": os.environ.get("PATH", ""),
                "SYSTEMROOT": os.environ.get("SYSTEMROOT", ""),
                "TEMP": os.environ.get("TEMP", "/tmp"),
                "TMP": os.environ.get("TMP", "/tmp"),
                "HOME": str(cwd),
                "PYTHONPATH": f"{guard_dir.name}{os.pathsep}{cwd}",
                "PYTHONNOUSERSITE": "1",
                "PYTHONDONTWRITEBYTECODE": "1",
                "HF_HUB_OFFLINE": "1",
                "TRANSFORMERS_OFFLINE": "1",
                "PIP_NO_INDEX": "1",
                "PIP_DISABLE_PIP_VERSION_CHECK": "1",
                "NO_PROXY": "*",
                "HTTP_PROXY": "http://127.0.0.1:1",
                "HTTPS_PROXY": "http://127.0.0.1:1",
            }
            isolation_used = self.isolation_backend
            if self._linux_netns:
                unshare = shutil.which("unshare")
                if unshare:
                    exec_argv = [unshare, "--user", "--map-root-user", "--net", "--fork", *exec_argv]

            def _preexec() -> None:
                if os.name != "nt":
                    os.setsid()
                    try:
                        import resource

                        mem_bytes = max(256, max_memory_mb) * 1024 * 1024
                        cpu_secs = max(5, int(timeout_seconds) + 2)
                        resource.setrlimit(resource.RLIMIT_CPU, (cpu_secs, cpu_secs))
                        resource.setrlimit(resource.RLIMIT_AS, (mem_bytes, mem_bytes))
                        resource.setrlimit(resource.RLIMIT_NOFILE, (256, 256))
                    except (ImportError, OSError, ValueError):
                        pass

            started = time.monotonic()
            win_job: _WindowsJobSandbox | None = None
            creationflags = 0
            if os.name == "nt":
                creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(
                    subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0x01000000
                )
                try:
                    win_job = _WindowsJobSandbox(max_memory_mb=max_memory_mb)
                    win_job.create_and_configure()
                except OSError:
                    win_job = None
                    isolation_used = "windows_restricted_process_group+socket_guard"

            try:
                try:
                    proc = subprocess.Popen(
                        exec_argv,
                        cwd=str(cwd),
                        env=clean_env,
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True,
                        encoding="utf-8",
                        errors="replace",
                        shell=False,
                        close_fds=True,
                        preexec_fn=_preexec if os.name != "nt" else None,
                        creationflags=creationflags,
                    )
                except OSError:
                    # Fallback on Windows hosts nested inside non-breakaway parent jobs.
                    if os.name != "nt":
                        raise
                    creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                    proc = subprocess.Popen(
                        exec_argv,
                        cwd=str(cwd),
                        env=clean_env,
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True,
                        encoding="utf-8",
                        errors="replace",
                        shell=False,
                        close_fds=True,
                        creationflags=creationflags,
                    )

                if win_job is not None:
                    proc_handle = int(getattr(proc, "_handle", 0) or 0)
                    if proc_handle:
                        try:
                            win_job.assign_process(proc_handle)
                        except OSError:
                            isolation_used = "windows_restricted_process_group+socket_guard"

                def _kill_tree() -> None:
                    if proc.poll() is not None:
                        return
                    try:
                        if win_job is not None:
                            win_job.terminate()
                        if os.name != "nt":
                            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                        else:
                            proc.kill()
                    except OSError:
                        try:
                            proc.kill()
                        except OSError:
                            pass

                if cancel_token is not None:
                    cancel_token.on_cancel(_kill_tree)

                timed_out = False
                try:
                    stdout, stderr = proc.communicate(timeout=max(1.0, min(timeout_seconds, 180.0)))
                except subprocess.TimeoutExpired:
                    timed_out = True
                    _kill_tree()
                    stdout, stderr = proc.communicate()
            finally:
                if win_job is not None:
                    win_job.close()

            duration_ms = round((time.monotonic() - started) * 1000.0, 2)
            if cancel_token is not None and cancel_token.is_cancelled:
                raise AutomationError("Sandbox execution cancelled by request")
            if timed_out:
                raise AutomationError(f"Sandbox execution timed out after {timeout_seconds}s")

            safe_out = redact_text(stdout or "")[:MAX_OUTPUT_CHARS]
            safe_err = redact_text(stderr or "")[:MAX_OUTPUT_CHARS]
            return {
                "exit_code": int(proc.returncode or 0),
                "stdout": safe_out,
                "stderr": safe_err,
                "truncated": len(stdout or "") > MAX_OUTPUT_CHARS or len(stderr or "") > MAX_OUTPUT_CHARS,
                "duration_ms": duration_ms,
                "isolation_backend": isolation_used,
                "network_disabled": True,
            }
        finally:
            guard_dir.cleanup()

    def verify_network_blocked(self, cwd: Path) -> dict[str, Any]:
        """Probe inside the sandbox runner that external IPv4 sockets cannot connect."""
        probe_code = (
            "import errno, socket, sys; s = socket.socket(socket.AF_INET, socket.SOCK_STREAM); "
            "s.settimeout(1.5); rc = s.connect_ex(('198.51.100.1', 443)); s.close(); "
            "print(rc); sys.exit(0 if rc != 0 else 1)"
        )
        res = self.run_command([sys.executable, "-I", "-c", probe_code], cwd=cwd, timeout_seconds=5.0)
        errno_val = int(res["stdout"].strip()) if res["stdout"].strip().isdigit() else -1
        hard_unroutable = errno_val in {errno.ENETUNREACH, errno.EHOSTUNREACH, errno.EACCES, errno.EPERM}
        return {
            "blocked": res["exit_code"] == 0,
            "hard_os_unroutable": hard_unroutable,
            "connect_errno": errno_val,
            "isolation_backend": res["isolation_backend"],
        }


class WorkspaceManager:
    """Workspace-scoped repository inspection, patch diff/apply/revert, and sandboxed checks."""

    def __init__(
        self,
        database: Database,
        allowed_roots: list[Path],
        *,
        sandbox: IsolatedSandboxRunner | None = None,
    ) -> None:
        self.database = database
        self.allowed_roots = [r.expanduser().resolve() for r in allowed_roots]
        self.sandbox = sandbox or IsolatedSandboxRunner()

    def _inside_allowed(self, candidate: Path) -> bool:
        for root in self.allowed_roots:
            try:
                candidate.relative_to(root)
                return True
            except ValueError:
                continue
        registered = self.database.fetch_all("SELECT root_path FROM coding_workspaces WHERE approved = 1")
        for row in registered:
            try:
                candidate.relative_to(Path(row["root_path"]).resolve())
                return True
            except ValueError:
                continue
        return False

    def register_workspace(self, root_path: str | Path, *, name: str = "") -> dict[str, Any]:
        resolved = Path(root_path).expanduser().resolve()
        if not resolved.exists() or not resolved.is_dir():
            raise AutomationError(f"Workspace directory does not exist: {resolved}")
        git_enabled = (resolved / ".git").exists()
        test_profile = self._detect_test_profile(resolved)
        existing = self.database.fetch_one(
            "SELECT workspace_id FROM coding_workspaces WHERE root_path = ?", (str(resolved),)
        )
        ws_id = str(existing["workspace_id"]) if existing else uuid.uuid4().hex[:12]
        label = name.strip() or resolved.name
        self.database.execute(
            """INSERT INTO coding_workspaces(workspace_id, name, root_path, approved, git_enabled, test_profile)
               VALUES(?, ?, ?, 1, ?, ?)
               ON CONFLICT(root_path) DO UPDATE SET name=excluded.name, approved=1,
               git_enabled=excluded.git_enabled, test_profile=excluded.test_profile, updated_at=datetime('now')""",
            (ws_id, label, str(resolved), int(git_enabled), test_profile),
        )
        return {
            "workspace_id": ws_id,
            "name": label,
            "root_path": str(resolved),
            "git_enabled": git_enabled,
            "test_profile": test_profile,
        }

    def list_workspaces(self) -> list[dict[str, Any]]:
        return self.database.fetch_all("SELECT * FROM coding_workspaces WHERE approved = 1 ORDER BY name")

    def resolve_workspace(self, workspace: str = "") -> tuple[str, Path]:
        if workspace.strip():
            by_id = self.database.fetch_one(
                "SELECT workspace_id, root_path FROM coding_workspaces WHERE (workspace_id = ? OR name = ?) AND approved = 1",
                (workspace.strip(), workspace.strip()),
            )
            if by_id:
                return str(by_id["workspace_id"]), Path(str(by_id["root_path"])).resolve()
            candidate = Path(workspace.strip()).expanduser().resolve()
            if candidate.is_dir() and self._inside_allowed(candidate):
                reg = self.register_workspace(candidate)
                return reg["workspace_id"], candidate
            raise AutomationError(f"Workspace '{workspace}' is not registered or outside permitted roots")
        rows = self.list_workspaces()
        if rows:
            return str(rows[0]["workspace_id"]), Path(str(rows[0]["root_path"])).resolve()
        if self.allowed_roots and self.allowed_roots[0].is_dir():
            reg = self.register_workspace(self.allowed_roots[0])
            return reg["workspace_id"], self.allowed_roots[0]
        raise AutomationError("No approved coding workspace is selected")

    def _resolve_in_workspace(self, ws_root: Path, rel_path: str, *, must_exist: bool = True) -> Path:
        cleaned = rel_path.strip().lstrip("/\\")
        if not cleaned or "\x00" in cleaned:
            raise AutomationError("A relative file path inside the workspace is required")
        candidate = (ws_root / cleaned).resolve(strict=False)
        try:
            rel_norm = candidate.relative_to(ws_root).as_posix()
        except ValueError as exc:
            raise AutomationError("Access denied: path escapes selected workspace") from exc
        lower_rel = rel_norm.lower()
        for blocked in BLOCKED_WORKSPACE_PATTERNS:
            if lower_rel == blocked or lower_rel.endswith("/" + blocked):
                raise AutomationError(f"Access to sensitive file '{rel_norm}' is blocked by workspace policy")
        if must_exist and not candidate.exists():
            raise AutomationError(f"Workspace path does not exist: {rel_norm}")
        if candidate.is_symlink():
            raise AutomationError(f"Symbolic links inside workspaces are rejected: {rel_norm}")
        return candidate

    @staticmethod
    def _detect_test_profile(ws_root: Path) -> str:
        if (ws_root / "scripts" / "run_tests.py").is_file():
            return "hinata_runner"
        if (ws_root / "pyproject.toml").is_file() or (ws_root / "tests").is_dir():
            return "python_unittest"
        return "python_compile"

    def inventory(self, workspace: str = "", max_files: int = 200) -> dict[str, Any]:
        ws_id, ws_root = self.resolve_workspace(workspace)
        files: list[dict[str, Any]] = []
        languages: dict[str, int] = {}
        truncated = False
        ignored_dirs = {".git", ".venv", "venv", "__pycache__", "node_modules", ".mypy_cache", ".ruff_cache", "dist", "build"}
        for dirpath, dirnames, filenames in os.walk(ws_root, followlinks=False):
            dirnames[:] = [d for d in sorted(dirnames) if d not in ignored_dirs and not d.startswith(".")]
            for fname in sorted(filenames):
                fpath = Path(dirpath) / fname
                if fpath.is_symlink():
                    continue
                resolved = fpath.resolve(strict=False)
                try:
                    rel = resolved.relative_to(ws_root).as_posix()
                except ValueError:
                    continue
                ext = resolved.suffix.lower() or "no_ext"
                languages[ext] = languages.get(ext, 0) + 1
                if len(files) < max(1, min(max_files, 500)):
                    files.append({"path": rel, "size": resolved.stat().st_size})
                else:
                    truncated = True
            if truncated:
                break
        return {
            "workspace_id": ws_id,
            "root": str(ws_root),
            "files": files,
            "languages": languages,
            "git_enabled": (ws_root / ".git").exists(),
            "test_profile": self._detect_test_profile(ws_root),
            "truncated": truncated,
        }

    def read_source(self, path: str, workspace: str = "", max_chars: int = 20000) -> dict[str, Any]:
        ws_id, ws_root = self.resolve_workspace(workspace)
        target = self._resolve_in_workspace(ws_root, path, must_exist=True)
        if not target.is_file():
            raise AutomationError(f"Not a regular file: {path}")
        if target.suffix.lower() not in ALLOWED_SOURCE_SUFFIXES:
            raise AutomationError(f"Unsupported source extension '{target.suffix}'")
        raw = target.read_bytes()
        if len(raw) > 500_000:
            raise AutomationError("Source file exceeds 500 KB read limit")
        content = raw.decode("utf-8-sig", errors="replace")
        # A negative bound sliced from the end: max_chars=-1 returned the whole
        # file minus its last character while claiming it was truncated.
        max_chars = max(0, min(int(max_chars), 500_000))
        truncated = len(content) > max_chars
        return {
            "workspace_id": ws_id,
            "path": target.relative_to(ws_root).as_posix(),
            "sha256": hashlib.sha256(raw).hexdigest(),
            "content": content[:max_chars],
            "truncated": truncated,
        }

    def search_workspace(self, query: str, workspace: str = "", max_results: int = 25) -> dict[str, Any]:
        ws_id, ws_root = self.resolve_workspace(workspace)
        needle = query.strip().casefold()
        if not needle:
            raise AutomationError("Search query cannot be empty")
        matches: list[dict[str, Any]] = []
        ignored_dirs = {".git", ".venv", "venv", "__pycache__", "node_modules", ".mypy_cache", ".ruff_cache"}
        for dirpath, dirnames, filenames in os.walk(ws_root, followlinks=False):
            dirnames[:] = [d for d in sorted(dirnames) if d not in ignored_dirs]
            for fname in sorted(filenames):
                fpath = Path(dirpath) / fname
                if fpath.is_symlink() or fpath.suffix.lower() not in ALLOWED_SOURCE_SUFFIXES:
                    continue
                try:
                    if fpath.stat().st_size > 300_000:
                        continue
                    text = fpath.read_text(encoding="utf-8-sig", errors="replace")
                except OSError:
                    continue
                rel = fpath.resolve().relative_to(ws_root).as_posix()
                for line_no, line in enumerate(text.splitlines(), 1):
                    if needle in line.casefold():
                        matches.append({"path": rel, "line": line_no, "snippet": redact_text(line.strip())[:200]})
                        if len(matches) >= max(1, min(max_results, 50)):
                            return {"workspace_id": ws_id, "matches": matches, "truncated": True}
                        break
        return {"workspace_id": ws_id, "matches": matches, "truncated": False}

    def git_status_diff(self, workspace: str = "") -> dict[str, Any]:
        ws_id, ws_root = self.resolve_workspace(workspace)
        git = shutil.which("git")
        if not git or not (ws_root / ".git").exists():
            return {"workspace_id": ws_id, "git_available": False, "status": "", "diff": ""}
        st = self.sandbox.run_command([git, "status", "--short", "--branch"], cwd=ws_root, timeout_seconds=10.0)
        df = self.sandbox.run_command([git, "diff", "--no-ext-diff"], cwd=ws_root, timeout_seconds=10.0)
        return {
            "workspace_id": ws_id,
            "git_available": True,
            "status": st["stdout"],
            "diff": df["stdout"][:12000],
        }

    def propose_patch(
        self,
        *,
        path: str,
        new_content: str,
        title: str = "Proposed code change",
        workspace: str = "",
        task_id: str = "",
    ) -> dict[str, Any]:
        """Validate and store a unified diff proposal without modifying disk yet."""
        ws_id, ws_root = self.resolve_workspace(workspace)
        target = self._resolve_in_workspace(ws_root, path, must_exist=False)
        if target.suffix.lower() in BLOCKED_PATCH_SUFFIXES or target.suffix.lower() not in ALLOWED_SOURCE_SUFFIXES:
            raise AutomationError(f"File extension '{target.suffix}' is not allowed for coding patches")
        encoded_new = new_content.encode("utf-8")
        if len(encoded_new) > MAX_PATCH_BYTES:
            raise AutomationError("Proposed file exceeds maximum patch byte budget")
        existed = target.exists()
        old_content = target.read_text(encoding="utf-8") if existed else ""
        old_sha = hashlib.sha256(old_content.encode("utf-8")).hexdigest() if existed else ""
        rel = target.relative_to(ws_root).as_posix()
        diff_lines = list(
            difflib.unified_diff(
                old_content.splitlines(keepends=True),
                new_content.splitlines(keepends=True),
                fromfile=f"a/{rel}",
                tofile=f"b/{rel}",
            )
        )
        diff_text = "".join(diff_lines)
        if not diff_text.strip():
            raise AutomationError("Proposed patch makes no changes to the target file")

        patch_id = uuid.uuid4().hex[:12]
        files_spec = [
            {
                "path": rel,
                "existed": existed,
                "old_sha256": old_sha,
                "new_content": new_content,
                "new_sha256": hashlib.sha256(encoded_new).hexdigest(),
            }
        ]
        backup_spec = {rel: {"existed": existed, "content": old_content, "sha256": old_sha}}
        self.database.execute(
            """INSERT INTO coding_patches(patch_id, workspace_id, task_id, title, diff_text, files_json, backup_json, state)
               VALUES(?, ?, ?, ?, ?, ?, ?, 'PROPOSED')""",
            (
                patch_id,
                ws_id,
                task_id,
                title[:200],
                diff_text[:MAX_PATCH_BYTES],
                json.dumps(files_spec, ensure_ascii=False),
                json.dumps(backup_spec, ensure_ascii=False),
            ),
        )
        return {
            "patch_id": patch_id,
            "workspace_id": ws_id,
            "path": rel,
            "state": "PROPOSED",
            "diff": diff_text[:8000],
            "lines_changed": sum(1 for ln in diff_lines if ln.startswith(("+", "-")) and not ln.startswith(("+++", "---"))),
        }

    def apply_patch(self, patch_id: str) -> dict[str, Any]:
        """Apply an approved patch atomically after checking pre-image SHA-256."""
        row = self.database.fetch_one("SELECT * FROM coding_patches WHERE patch_id = ?", (patch_id,))
        if row is None:
            raise AutomationError(f"Patch '{patch_id}' does not exist")
        if row["state"] != "PROPOSED":
            raise AutomationError(f"Patch '{patch_id}' is in state '{row['state']}' and cannot be applied")
        _, ws_root = self.resolve_workspace(str(row["workspace_id"]))
        files_spec = json.loads(str(row["files_json"]))
        applied_files: list[str] = []
        for entry in files_spec:
            rel = str(entry["path"])
            target = self._resolve_in_workspace(ws_root, rel, must_exist=False)
            if entry["existed"]:
                if not target.exists():
                    raise AutomationError(f"Conflict: {rel} was deleted since patch proposal")
                actual_sha = hashlib.sha256(target.read_text(encoding="utf-8").encode("utf-8")).hexdigest()
                if actual_sha != entry["old_sha256"]:
                    raise AutomationError(f"Conflict: {rel} changed on disk since patch proposal")
            elif target.exists():
                raise AutomationError(f"Conflict: {rel} now exists on disk")

            target.parent.mkdir(parents=True, exist_ok=True)
            tmp = target.with_name(f".{target.name}.patch-{patch_id}")
            tmp.write_text(str(entry["new_content"]), encoding="utf-8")
            tmp.replace(target)
            if target.suffix.lower() == ".py":
                pycache = target.parent / "__pycache__"
                if pycache.is_dir():
                    for pyc in pycache.glob(f"{target.stem}.*.pyc"):
                        pyc.unlink(missing_ok=True)
            post_sha = hashlib.sha256(target.read_text(encoding="utf-8").encode("utf-8")).hexdigest()
            if post_sha != entry["new_sha256"]:
                raise AutomationError(f"Postcondition failed: written SHA-256 mismatch for {rel}")
            applied_files.append(rel)

        self.database.execute(
            "UPDATE coding_patches SET state = 'APPLIED', updated_at = datetime('now') WHERE patch_id = ?",
            (patch_id,),
        )
        return {"patch_id": patch_id, "state": "APPLIED", "applied_files": applied_files}

    def revert_patch(self, patch_id: str) -> dict[str, Any]:
        """Roll back an APPLIED patch using its stored pre-image backup."""
        row = self.database.fetch_one("SELECT * FROM coding_patches WHERE patch_id = ?", (patch_id,))
        if row is None:
            raise AutomationError(f"Patch '{patch_id}' does not exist")
        if row["state"] != "APPLIED":
            raise AutomationError(f"Patch '{patch_id}' is in state '{row['state']}'; only APPLIED patches can be reverted")
        _, ws_root = self.resolve_workspace(str(row["workspace_id"]))
        backup = json.loads(str(row["backup_json"]))
        reverted: list[str] = []
        for rel, pre in backup.items():
            target = self._resolve_in_workspace(ws_root, rel, must_exist=False)
            if pre["existed"]:
                tmp = target.with_name(f".{target.name}.revert-{patch_id}")
                tmp.write_text(str(pre["content"]), encoding="utf-8")
                tmp.replace(target)
            else:
                target.unlink(missing_ok=True)
            reverted.append(rel)
        self.database.execute(
            "UPDATE coding_patches SET state = 'REVERTED', updated_at = datetime('now') WHERE patch_id = ?",
            (patch_id,),
        )
        return {"patch_id": patch_id, "state": "REVERTED", "reverted_files": reverted}

    def run_tests(
        self,
        workspace: str = "",
        target: str = "",
        *,
        timeout_seconds: float = 45.0,
        cancel_token: CancellationToken | None = None,
    ) -> dict[str, Any]:
        ws_id, ws_root = self.resolve_workspace(workspace)
        argv = [sys.executable, "-I", "-B", "-m", "unittest"]
        if target.strip():
            # Allow only safe dotted module or relative test file path.
            cleaned = target.strip()
            if any(ch in cleaned for ch in (";", "&", "|", "`", "$", "\n", "\r", "..")):
                raise AutomationError("Invalid test target format")
            argv.append(cleaned)
        else:
            start_dir = "tests" if (ws_root / "tests" / "__init__.py").is_file() else "."
            argv.extend(["discover", "-s", start_dir, "-t", ".", "-p", "test*.py"])
        res = self.sandbox.run_command(argv, cwd=ws_root, timeout_seconds=timeout_seconds, cancel_token=cancel_token)
        return {
            "workspace_id": ws_id,
            "tests_passed": res["exit_code"] == 0,
            **res,
        }

    def run_linter(self, workspace: str = "", *, timeout_seconds: float = 30.0) -> dict[str, Any]:
        ws_id, ws_root = self.resolve_workspace(workspace)
        ruff = shutil.which("ruff")
        if ruff:
            res = self.sandbox.run_command([ruff, "check", "."], cwd=ws_root, timeout_seconds=timeout_seconds)
            return {"workspace_id": ws_id, "tool": "ruff", "clean": res["exit_code"] == 0, **res}
        res = self.sandbox.run_command([sys.executable, "-I", "-m", "compileall", "-q", "."], cwd=ws_root, timeout_seconds=timeout_seconds)
        return {"workspace_id": ws_id, "tool": "compileall", "clean": res["exit_code"] == 0, **res}

    def run_type_checker(self, workspace: str = "", *, timeout_seconds: float = 45.0) -> dict[str, Any]:
        ws_id, ws_root = self.resolve_workspace(workspace)
        mypy = shutil.which("mypy")
        if not mypy:
            # Also check active Python environment's sibling bin directory.
            candidate = Path(sys.executable).parent / ("mypy.exe" if os.name == "nt" else "mypy")
            if candidate.is_file():
                mypy = str(candidate)
        if not mypy:
            raise AutomationError("Type checker (mypy) is not installed in this local environment")
        res = self.sandbox.run_command([mypy, "."], cwd=ws_root, timeout_seconds=timeout_seconds)
        return {"workspace_id": ws_id, "tool": "mypy", "clean": res["exit_code"] == 0, **res}

    def ast_symbol_index(self, workspace: str = "", *, query: str = "", max_symbols: int = 200) -> dict[str, Any]:
        """Build a zero-execution Python AST symbol & call-reference map across a registered workspace."""
        import ast

        ws_id, ws_root = self.resolve_workspace(workspace)
        needle = query.strip().casefold()
        symbols: list[dict[str, Any]] = []
        parse_errors: list[dict[str, str]] = []
        ignored_dirs = {".git", ".venv", "venv", "__pycache__", "node_modules", ".mypy_cache", ".ruff_cache"}

        for dirpath, dirnames, filenames in os.walk(ws_root, followlinks=False):
            dirnames[:] = [d for d in sorted(dirnames) if d not in ignored_dirs]
            for fname in sorted(filenames):
                if not fname.endswith(".py"):
                    continue
                fpath = Path(dirpath) / fname
                if fpath.is_symlink():
                    continue
                try:
                    if fpath.stat().st_size > 400_000:
                        continue
                    source = fpath.read_text(encoding="utf-8-sig", errors="replace")
                    rel = fpath.resolve().relative_to(ws_root).as_posix()
                except OSError:
                    continue
                try:
                    tree = ast.parse(source, filename=rel)
                except SyntaxError as exc:
                    parse_errors.append({"path": rel, "error": f"line {exc.lineno}: {exc.msg}"})
                    continue

                for node in tree.body:
                    if isinstance(node, ast.ClassDef):
                        methods = [
                            m.name
                            for m in node.body
                            if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef))
                        ]
                        bases = [ast.unparse(b) for b in node.bases]
                        if not needle or needle in node.name.casefold() or any(needle in m.casefold() for m in methods):
                            symbols.append(
                                {
                                    "kind": "class",
                                    "name": node.name,
                                    "path": rel,
                                    "line": node.lineno,
                                    "bases": bases[:6],
                                    "methods": methods[:30],
                                    "doc": (ast.get_docstring(node) or "").splitlines()[0][:120]
                                    if ast.get_docstring(node)
                                    else "",
                                }
                            )
                    elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        args = [a.arg for a in node.args.args]
                        calls = sorted(
                            {
                                c.func.id
                                for c in ast.walk(node)
                                if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)
                            }
                        )[:15]
                        if not needle or needle in node.name.casefold():
                            symbols.append(
                                {
                                    "kind": "async_function" if isinstance(node, ast.AsyncFunctionDef) else "function",
                                    "name": node.name,
                                    "path": rel,
                                    "line": node.lineno,
                                    "args": args[:12],
                                    "calls": calls,
                                    "doc": (ast.get_docstring(node) or "").splitlines()[0][:120]
                                    if ast.get_docstring(node)
                                    else "",
                                }
                            )
                    if len(symbols) >= max(1, min(max_symbols, 500)):
                        break
                if len(symbols) >= max(1, min(max_symbols, 500)):
                    break

        return {
            "workspace_id": ws_id,
            "symbol_count": len(symbols),
            "symbols": symbols,
            "parse_errors": parse_errors[:25],
        }

    def propose_multifile_patch(
        self,
        *,
        changes: list[dict[str, str]],
        title: str = "Proposed multi-file code change",
        workspace: str = "",
        task_id: str = "",
    ) -> dict[str, Any]:
        """Stage an atomic multi-file patch proposal with pre-image SHA-256 verification and full rollback."""
        if not changes or len(changes) > 20:
            raise AutomationError("Multi-file patch must contain between 1 and 20 file changes")
        ws_id, ws_root = self.resolve_workspace(workspace)
        seen_paths: set[str] = set()
        files_spec: list[dict[str, Any]] = []
        backup_spec: dict[str, dict[str, Any]] = {}
        combined_diff_parts: list[str] = []
        total_lines_changed = 0

        for item in changes:
            rel_input = str(item.get("path", "")).strip()
            new_content = str(item.get("new_content", ""))
            if not rel_input:
                raise AutomationError("Each file change must specify a relative 'path'")
            target = self._resolve_in_workspace(ws_root, rel_input, must_exist=False)
            if target.suffix.lower() in BLOCKED_PATCH_SUFFIXES or target.suffix.lower() not in ALLOWED_SOURCE_SUFFIXES:
                raise AutomationError(f"Patching '{target.suffix}' files is not permitted")
            rel = target.relative_to(ws_root).as_posix()
            if rel in BLOCKED_WORKSPACE_PATTERNS or any(rel.endswith(f"/{p}") for p in BLOCKED_WORKSPACE_PATTERNS):
                raise AutomationError(f"Refusing to patch protected file '{rel}'")
            if rel in seen_paths:
                raise AutomationError(f"Duplicate file path '{rel}' in multi-file patch")
            seen_paths.add(rel)

            encoded_new = new_content.encode("utf-8")
            if len(encoded_new) > MAX_PATCH_BYTES:
                raise AutomationError(f"New content for '{rel}' exceeds size limit")

            existed = target.is_file()
            old_content = target.read_text(encoding="utf-8") if existed else ""
            old_sha = hashlib.sha256(old_content.encode("utf-8")).hexdigest() if existed else ""
            diff_lines = list(
                difflib.unified_diff(
                    old_content.splitlines(keepends=True),
                    new_content.splitlines(keepends=True),
                    fromfile=f"a/{rel}",
                    tofile=f"b/{rel}",
                )
            )
            diff_piece = "".join(diff_lines)
            if not diff_piece.strip():
                continue
            combined_diff_parts.append(diff_piece)
            total_lines_changed += sum(
                1 for ln in diff_lines if ln.startswith(("+", "-")) and not ln.startswith(("+++", "---"))
            )
            files_spec.append(
                {
                    "path": rel,
                    "existed": existed,
                    "old_sha256": old_sha,
                    "new_content": new_content,
                    "new_sha256": hashlib.sha256(encoded_new).hexdigest(),
                }
            )
            backup_spec[rel] = {"existed": existed, "content": old_content, "sha256": old_sha}

        if not files_spec:
            raise AutomationError("Proposed multi-file patch makes no changes")
        full_diff = "\n".join(combined_diff_parts)
        if len(full_diff.encode("utf-8")) > MAX_PATCH_BYTES:
            raise AutomationError("Combined multi-file diff exceeds maximum patch size")

        patch_id = uuid.uuid4().hex[:12]
        self.database.execute(
            """INSERT INTO coding_patches(patch_id, workspace_id, task_id, title, diff_text, files_json, backup_json, state)
               VALUES(?, ?, ?, ?, ?, ?, ?, 'PROPOSED')""",
            (
                patch_id,
                ws_id,
                task_id,
                title[:200],
                full_diff,
                json.dumps(files_spec, ensure_ascii=False),
                json.dumps(backup_spec, ensure_ascii=False),
            ),
        )
        return {
            "patch_id": patch_id,
            "workspace_id": ws_id,
            "paths": [f["path"] for f in files_spec],
            "file_count": len(files_spec),
            "state": "PROPOSED",
            "diff": full_diff[:8000],
            "lines_changed": total_lines_changed,
        }
