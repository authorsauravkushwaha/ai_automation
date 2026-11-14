"""Bounded, read-only Python source inspection. Never imports or executes code."""
from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

from hinata.automation.app_control import AutomationError
from hinata.automation.file_manager import FileManager

MAX_SOURCE_CHARS = 20_000
MAX_ITEMS = 100


class CodeInspector:
    def __init__(self, files: FileManager) -> None:
        self.files = files

    def inspect(self, value: str) -> dict[str, Any]:
        path = self.files.resolve(value)
        if path.suffix.lower() != ".py":
            raise AutomationError("Code inspection currently supports Python .py files only")
        data = self.files.read_text(str(path), MAX_SOURCE_CHARS)
        if data["truncated"]:
            raise AutomationError("Python source exceeds the inspection limit; no partial analysis was returned")
        try:
            tree = ast.parse(data["content"], filename=str(path))
        except SyntaxError as exc:
            return {"path": str(path), "valid_syntax": False,
                    "syntax_error": {"line": exc.lineno, "message": exc.msg[:200]},
                    "functions": [], "classes": [], "imports": [], "warnings": [], "truncated": False}
        except (RecursionError, MemoryError, ValueError) as exc:
            raise AutomationError("Python source exceeds parser limits") from exc
        functions, classes, imports, warnings = [], [], [], []
        totals = {"functions": 0, "classes": 0, "imports": 0, "warnings": 0}
        def add(kind: str, collection: list, item: dict) -> None:
            totals[kind] += 1
            if len(collection) < MAX_ITEMS:
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
                if name in {"eval", "exec", "os.system", "pickle.load", "pickle.loads"}:
                    label = f"Potentially unsafe call: {name}"
                elif name in {"subprocess.run", "subprocess.Popen", "subprocess.call", "subprocess.check_call", "subprocess.check_output"}:
                    if any(kw.arg == "shell" and isinstance(kw.value, ast.Constant) and kw.value.value is True for kw in node.keywords):
                        label = "Subprocess call uses shell=True"
                if label:
                    add("warnings", warnings, {"line": node.lineno, "message": label})
        return {"path": str(path), "valid_syntax": True, "syntax_error": None,
                "functions": functions, "classes": classes, "imports": imports,
                "warnings": warnings, "truncated": any(total > MAX_ITEMS for total in totals.values())}

    @staticmethod
    def _call_name(node: ast.expr) -> str:
        if isinstance(node, ast.Name):
            return node.id
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            return node.value.id + "." + node.attr
        return ""
