"""Bounded, read-only Python source inspection and safe coding workflow helpers.

Never imports or executes user source code inside the assistant process.
"""
from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

from hinata.automation.app_control import AutomationError
from hinata.automation.file_manager import FileManager
from hinata.automation.workspace import WorkspaceManager

MAX_SOURCE_CHARS = 80_000
MAX_ITEMS = 200


class CodeInspector:
    def __init__(self, files: FileManager, workspace_manager: WorkspaceManager | None = None) -> None:
        self.files = files
        self.workspace_manager = workspace_manager

    def inspect(self, value: str, *, max_chars: int = 20_000) -> dict[str, Any]:
        path = self.files.resolve(value)
        if path.suffix.lower() != ".py":
            raise AutomationError("Code inspection currently supports Python .py files only")
        cap = max(1000, min(max_chars, MAX_SOURCE_CHARS))
        data = self.files.read_text(str(path), cap)
        if data["truncated"]:
            raise AutomationError("Python source exceeds the inspection limit; no partial analysis was returned")
        return self._parse_python_source(str(path), data["content"])

    def inspect_repository_imports(self, workspace: str = "") -> dict[str, Any]:
        """Build a bounded Python module import graph across an approved workspace."""
        if self.workspace_manager is None:
            raise AutomationError("Workspace manager is not configured")
        inv = self.workspace_manager.inventory(workspace, max_files=150)
        ws_root = Path(inv["root"])
        modules: dict[str, list[str]] = {}
        syntax_errors: list[dict[str, Any]] = []
        for item in inv["files"]:
            rel = str(item["path"])
            if not rel.endswith(".py"):
                continue
            full = ws_root / rel
            try:
                if full.stat().st_size > MAX_SOURCE_CHARS:
                    continue
                content = full.read_text(encoding="utf-8-sig", errors="replace")
            except OSError:
                continue
            parsed = self._parse_python_source(rel, content)
            if not parsed["valid_syntax"]:
                syntax_errors.append({"path": rel, "error": parsed["syntax_error"]})
                continue
            modules[rel] = sorted({imp["module"] for imp in parsed["imports"] if imp.get("module")})
        return {
            "workspace_id": inv["workspace_id"],
            "python_files_analyzed": len(modules) + len(syntax_errors),
            "import_graph": modules,
            "syntax_errors": syntax_errors,
        }

    def _parse_python_source(self, label_path: str, source: str) -> dict[str, Any]:
        try:
            tree = ast.parse(source, filename=label_path)
        except SyntaxError as exc:
            return {"path": label_path, "valid_syntax": False,
                    "syntax_error": {"line": exc.lineno, "message": exc.msg[:200]},
                    "functions": [], "classes": [], "imports": [], "warnings": [], "truncated": False}
        except (RecursionError, MemoryError, ValueError) as exc:
            raise AutomationError("Python source exceeds parser limits") from exc
        functions: list[dict[str, Any]] = []
        classes: list[dict[str, Any]] = []
        imports: list[dict[str, Any]] = []
        warnings: list[dict[str, Any]] = []
        totals = {"functions": 0, "classes": 0, "imports": 0, "warnings": 0}
        max_items = 100

        def add(kind: str, collection: list, item: dict) -> None:
            totals[kind] += 1
            if len(collection) < max_items:
                collection.append(item)

        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                add("functions", functions, {"name": node.name, "line": node.lineno, "async": isinstance(node, ast.AsyncFunctionDef)})
            elif isinstance(node, ast.ClassDef):
                add("classes", classes, {"name": node.name, "line": node.lineno})
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    add("imports", imports, {"module": alias.name, "line": node.lineno})
            elif isinstance(node, ast.ImportFrom):
                add("imports", imports, {"module": "." * node.level + (node.module or ""), "line": node.lineno})
            elif isinstance(node, ast.Call):
                name = self._call_name(node.func)
                label = None
                if name in {"eval", "exec", "os.system", "pickle.load", "pickle.loads", "marshal.loads"}:
                    label = f"Potentially unsafe call: {name}"
                elif name in {"subprocess.run", "subprocess.Popen", "subprocess.call", "subprocess.check_call", "subprocess.check_output"}:
                    if any(kw.arg == "shell" and isinstance(kw.value, ast.Constant) and kw.value.value is True for kw in node.keywords):
                        label = "Subprocess call uses shell=True"
                if label:
                    add("warnings", warnings, {"line": node.lineno, "message": label})
        return {"path": label_path, "valid_syntax": True, "syntax_error": None,
                "functions": functions, "classes": classes, "imports": imports,
                "warnings": warnings, "truncated": any(total > max_items for total in totals.values())}

    @staticmethod
    def _call_name(node: ast.expr) -> str:
        if isinstance(node, ast.Name):
            return node.id
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            return node.value.id + "." + node.attr
        return ""
