"""Bounded static code inspection AND polyglot code authoring in any programming language.

Never imports or executes user source code inside the assistant process.
"""

from __future__ import annotations

import ast
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from hinata.automation.app_control import AutomationError
from hinata.automation.file_manager import FileManager
from hinata.automation.workspace import WorkspaceManager

MAX_SOURCE_CHARS = 80_000
MAX_ITEMS = 200


@dataclass(frozen=True, slots=True)
class LanguageSpec:
    canonical: str
    display_name: str
    fence_tag: str
    extension: str
    comment_prefix: str


LANGUAGE_SPECS: tuple[LanguageSpec, ...] = (
    LanguageSpec("python", "Python", "python", ".py", "#"),
    LanguageSpec("javascript", "JavaScript", "javascript", ".js", "//"),
    LanguageSpec("typescript", "TypeScript", "typescript", ".ts", "//"),
    LanguageSpec("cpp", "C++", "cpp", ".cpp", "//"),
    LanguageSpec("c", "C", "c", ".c", "//"),
    LanguageSpec("csharp", "C#", "csharp", ".cs", "//"),
    LanguageSpec("java", "Java", "java", ".java", "//"),
    LanguageSpec("rust", "Rust", "rust", ".rs", "//"),
    LanguageSpec("go", "Go", "go", ".go", "//"),
    LanguageSpec("kotlin", "Kotlin", "kotlin", ".kt", "//"),
    LanguageSpec("swift", "Swift", "swift", ".swift", "//"),
    LanguageSpec("php", "PHP", "php", ".php", "//"),
    LanguageSpec("ruby", "Ruby", "ruby", ".rb", "#"),
    LanguageSpec("html", "HTML5", "html", ".html", "<!--"),
    LanguageSpec("css", "CSS", "css", ".css", "/*"),
    LanguageSpec("sql", "SQL", "sql", ".sql", "--"),
    LanguageSpec("bash", "Bash", "bash", ".sh", "#"),
    LanguageSpec("powershell", "PowerShell", "powershell", ".ps1", "#"),
    LanguageSpec("dart", "Dart", "dart", ".dart", "//"),
    LanguageSpec("r", "R", "r", ".R", "#"),
    LanguageSpec("lua", "Lua", "lua", ".lua", "--"),
    LanguageSpec("scala", "Scala", "scala", ".scala", "//"),
    LanguageSpec("perl", "Perl", "perl", ".pl", "#"),
    LanguageSpec("haskell", "Haskell", "haskell", ".hs", "--"),
    LanguageSpec("elixir", "Elixir", "elixir", ".ex", "#"),
    LanguageSpec("zig", "Zig", "zig", ".zig", "//"),
    LanguageSpec("julia", "Julia", "julia", ".jl", "#"),
    LanguageSpec("matlab", "MATLAB", "matlab", ".m", "%"),
    LanguageSpec("assembly", "Assembly", "asm", ".asm", ";"),
    LanguageSpec("jsx", "React JSX", "jsx", ".jsx", "//"),
    LanguageSpec("tsx", "React TypeScript", "tsx", ".tsx", "//"),
    LanguageSpec("solidity", "Solidity", "solidity", ".sol", "//"),
)

LANGUAGE_ALIASES: dict[str, str] = {
    "py": "python",
    "python3": "python",
    "js": "javascript",
    "node": "javascript",
    "nodejs": "javascript",
    "ts": "typescript",
    "c++": "cpp",
    "cxx": "cpp",
    "cplusplus": "cpp",
    "c#": "csharp",
    "cs": "csharp",
    "dotnet": "csharp",
    "rs": "rust",
    "golang": "go",
    "kt": "kotlin",
    "rb": "ruby",
    "html5": "html",
    "web": "html",
    "sh": "bash",
    "shell": "bash",
    "zsh": "bash",
    "ps1": "powershell",
    "flutter": "dart",
    "rlang": "r",
    "pl": "perl",
    "hs": "haskell",
    "ex": "elixir",
    "jl": "julia",
    "asm": "assembly",
    "x86": "assembly",
    "react": "jsx",
    "mysql": "sql",
    "postgresql": "sql",
    "postgres": "sql",
    "sqlite": "sql",
    "sol": "solidity",
}

_SPEC_BY_KEY: dict[str, LanguageSpec] = {spec.canonical: spec for spec in LANGUAGE_SPECS}
_SPEC_BY_EXT: dict[str, LanguageSpec] = {spec.extension.lower(): spec for spec in LANGUAGE_SPECS}


def resolve_language(raw_language: str, output_path: str = "") -> LanguageSpec:
    """Resolve any programming language name, alias, or target file extension."""
    key = raw_language.strip().lower()
    if key in LANGUAGE_ALIASES:
        key = LANGUAGE_ALIASES[key]
    if key in _SPEC_BY_KEY:
        return _SPEC_BY_KEY[key]
    if output_path:
        ext = Path(output_path).suffix.lower()
        if ext in _SPEC_BY_EXT:
            return _SPEC_BY_EXT[ext]
    clean = re.sub(r"[^a-z0-9+#_-]+", "", key)[:32] or "python"
    if clean in LANGUAGE_ALIASES:
        clean = LANGUAGE_ALIASES[clean]
    if clean in _SPEC_BY_KEY:
        return _SPEC_BY_KEY[clean]
    return LanguageSpec(
        canonical=clean,
        display_name=raw_language.strip() or clean.title(),
        fence_tag=clean,
        extension=f".{re.sub(r'[^a-z0-9]+', '', clean) or 'txt'}",
        comment_prefix="//",
    )


def _extract_code_from_reply(raw_reply: str) -> tuple[str, str]:
    """Extract clean source code and optional brief explanation from model output."""
    cleaned = re.sub(r"<think>.*?</think>", "", raw_reply, flags=re.DOTALL | re.IGNORECASE).strip()
    fence_match = re.search(
        r"```[a-zA-Z0-9_+#.-]*\s*\n(.*?)```",
        cleaned,
        flags=re.DOTALL,
    )
    if fence_match:
        code = fence_match.group(1).rstrip() + "\n"
        before = cleaned[: fence_match.start()].strip()
        after = cleaned[fence_match.end() :].strip()
        notes = "\n\n".join(part for part in (before, after) if part)
        return code, notes[:1500]
    return cleaned.rstrip() + "\n", ""


class PolyglotCoder:
    """Writes clean, idiomatic code in any programming language and optionally saves to disk."""

    def __init__(self, files: FileManager, llm: Any = None) -> None:
        self.files = files
        self.llm = llm

    def generate_code(
        self,
        *,
        task: str,
        language: str = "python",
        output_path: str = "",
        overwrite: bool = False,
        llm_enabled: bool = True,
    ) -> dict[str, Any]:
        clean_task = task.strip()
        if not clean_task:
            raise AutomationError("Code specification/task must not be empty")
        spec = resolve_language(language, output_path=output_path)

        code = ""
        notes = ""
        generator = "offline_polyglot_template"

        if llm_enabled and self.llm is not None:
            system_prompt = (
                f"You are HINATA's expert polyglot software engineer. Write complete, clean, idiomatic, "
                f"well-structured {spec.display_name} code fulfilling the user's request.\n"
                f"Return the complete runnable {spec.display_name} source inside a single ```{spec.fence_tag} ... ``` block, "
                f"followed by a concise 1-3 sentence explanation of how to run or use it. "
                f"Do not truncate functions or leave placeholder TODO stubs."
            )
            keep_alive = getattr(getattr(self.llm, "config", None), "keep_alive", "5m")
            previous_code = ""
            syntax_issue = ""
            for attempt in range(2):
                user_prompt = f"Language: {spec.display_name}\nTask: {clean_task}"
                if attempt:
                    user_prompt += (
                        f"\n\nYour previous source failed the syntax check: {syntax_issue}. "
                        "Repair it and return the full corrected source, not a diff.\nPrevious source:\n"
                        f"{previous_code[:18000]}"
                    )
                try:
                    raw = self.llm.chat(
                        [{"role": "user", "content": user_prompt}],
                        system_prompt,
                        connect=True,
                        pull_missing=False,
                        keep_alive=keep_alive,
                        role="coder",
                        num_predict=1536,
                    )
                    extracted_code, extracted_notes = _extract_code_from_reply(raw)
                    if not extracted_code.strip():
                        break
                    previous_code = extracted_code
                    if spec.canonical == "python":
                        try:
                            ast.parse(extracted_code)
                        except SyntaxError as exc:
                            syntax_issue = f"line {exc.lineno}: {exc.msg}"
                            continue
                    elif spec.canonical == "json":
                        try:
                            json.loads(extracted_code)
                        except ValueError as exc:
                            syntax_issue = str(exc)[:200]
                            continue
                    code = extracted_code
                    notes = extracted_notes
                    generator = getattr(self.llm, "last_model_identity", "") or "local_coder"
                    break
                except Exception:
                    break

        if not code.strip():
            code = self._fallback_template(spec, clean_task)
            notes = (
                f"Generated clean {spec.display_name} starter implementation offline. "
                "Connect a local Ollama coding model (such as qwen2.5-coder) for full custom AI code synthesis."
            )

        syntax_valid: bool | None = None
        if spec.canonical == "python":
            try:
                ast.parse(code)
                syntax_valid = True
            except SyntaxError:
                syntax_valid = False
        elif spec.canonical == "json":
            try:
                json.loads(code)
                syntax_valid = True
            except ValueError:
                syntax_valid = False

        saved_path = ""
        bytes_written = 0
        if output_path.strip():
            write_res = self.files.write_text_file(output_path.strip(), code, overwrite=overwrite)
            saved_path = str(write_res["path"])
            bytes_written = int(write_res["bytes_written"])

        formatted = f"### {spec.display_name} — {clean_task[:120]}\n```{spec.fence_tag}\n{code.rstrip()}\n```"
        if saved_path:
            formatted += f"\n\nSaved to `{saved_path}` ({bytes_written} bytes)."
        if notes:
            formatted += f"\n\n{notes}"

        return {
            "language": spec.canonical,
            "display_language": spec.display_name,
            "extension": spec.extension,
            "task": clean_task,
            "code": code,
            "formatted": formatted,
            "generator": generator,
            "syntax_valid": syntax_valid,
            "saved_path": saved_path,
            "bytes_written": bytes_written,
        }

    @staticmethod
    def _fallback_template(spec: LanguageSpec, task: str) -> str:
        safe_desc = task.replace("\n", " ").strip()[:160]
        lang = spec.canonical
        if lang == "python":
            return (
                f'"""{safe_desc}"""\n\n'
                "from __future__ import annotations\n\n\n"
                "def solve(input_data: str = \"\") -> str:\n"
                f"    \"\"\"Implementation entry point for: {safe_desc}\"\"\"\n"
                "    return f\"Completed: {input_data or 'ready'}\"\n\n\n"
                "if __name__ == \"__main__\":\n"
                f"    print(solve({safe_desc!r}))\n"
            )
        if lang in {"javascript", "typescript"}:
            type_ann = ": string" if lang == "typescript" else ""
            ret_ann = ": string" if lang == "typescript" else ""
            return (
                f"// {safe_desc}\n\n"
                f"function runTask(label{type_ann} = {json.dumps(safe_desc)}){ret_ann} {{\n"
                "  return `Executed: ${label}`;\n"
                "}\n\n"
                "console.log(runTask());\n"
            )
        if lang == "cpp":
            return (
                f"// {safe_desc}\n"
                "#include <iostream>\n"
                "#include <string>\n\n"
                "int main() {\n"
                f"    std::string task = {json.dumps(safe_desc)};\n"
                "    std::cout << \"Running: \" << task << std::endl;\n"
                "    return 0;\n"
                "}\n"
            )
        if lang == "c":
            return (
                f"/* {safe_desc} */\n"
                "#include <stdio.h>\n\n"
                "int main(void) {\n"
                f"    printf(\"Running: %s\\n\", {json.dumps(safe_desc)});\n"
                "    return 0;\n"
                "}\n"
            )
        if lang == "java":
            return (
                f"// {safe_desc}\n"
                "public class Main {\n"
                "    public static void main(String[] args) {\n"
                f"        System.out.println({json.dumps('Running: ' + safe_desc)});\n"
                "    }\n"
                "}\n"
            )
        if lang == "csharp":
            return (
                f"// {safe_desc}\n"
                "using System;\n\n"
                "public static class Program {\n"
                "    public static void Main(string[] args) {\n"
                f"        Console.WriteLine({json.dumps('Running: ' + safe_desc)});\n"
                "    }\n"
                "}\n"
            )
        if lang == "rust":
            return (
                f"// {safe_desc}\n"
                "fn main() {\n"
                f"    let task = {json.dumps(safe_desc)};\n"
                "    println!(\"Running: {}\", task);\n"
                "}\n"
            )
        if lang == "go":
            return (
                f"// {safe_desc}\n"
                "package main\n\n"
                "import \"fmt\"\n\n"
                "func main() {\n"
                f"    fmt.Println({json.dumps('Running: ' + safe_desc)})\n"
                "}\n"
            )
        if lang == "html":
            import html as html_mod

            esc = html_mod.escape(safe_desc)
            return (
                "<!DOCTYPE html>\n"
                "<html lang=\"en\">\n"
                "<head>\n"
                "  <meta charset=\"UTF-8\" />\n"
                "  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1.0\" />\n"
                f"  <title>{esc}</title>\n"
                "</head>\n"
                "<body>\n"
                f"  <h1>{esc}</h1>\n"
                "</body>\n"
                "</html>\n"
            )
        if lang == "sql":
            return (
                f"-- {safe_desc}\n"
                "CREATE TABLE IF NOT EXISTS items (\n"
                "    id INTEGER PRIMARY KEY,\n"
                "    title TEXT NOT NULL,\n"
                "    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP\n"
                ");\n\n"
                "SELECT id, title, created_at FROM items ORDER BY id DESC;\n"
            )
        if lang == "bash":
            return (
                "#!/usr/bin/env bash\n"
                f"# {safe_desc}\n"
                "set -euo pipefail\n\n"
                f"echo {json.dumps('Running: ' + safe_desc)}\n"
            )
        if lang == "powershell":
            return (
                f"# {safe_desc}\n"
                "$ErrorActionPreference = 'Stop'\n"
                f"Write-Output {json.dumps('Running: ' + safe_desc)}\n"
            )
        if lang == "php":
            return (
                "<?php\n"
                "declare(strict_types=1);\n\n"
                f"// {safe_desc}\n"
                "function runTask(string $input = ''): string {\n"
                "    return 'Completed: ' . $input;\n"
                "}\n\n"
                f"echo runTask({json.dumps(safe_desc)}) . PHP_EOL;\n"
            )
        if lang == "ruby":
            return (
                f"# {safe_desc}\n"
                "def run_task(input = nil)\n"
                "  \"Completed: #{input || 'ready'}\"\n"
                "end\n\n"
                f"puts run_task({json.dumps(safe_desc)})\n"
            )
        if lang == "kotlin":
            return (
                f"// {safe_desc}\n"
                "fun main() {\n"
                f"    val task = {json.dumps(safe_desc)}\n"
                "    println(\"Running: $task\")\n"
                "}\n"
            )
        if lang == "swift":
            return (
                f"// {safe_desc}\n"
                "import Foundation\n\n"
                "@main\n"
                "struct Main {\n"
                "    static func main() {\n"
                f"        print({json.dumps('Running: ' + safe_desc)})\n"
                "    }\n"
                "}\n"
            )
        if lang == "dart":
            return (
                f"// {safe_desc}\n"
                "void main() {\n"
                f"  final task = {json.dumps(safe_desc)};\n"
                "  print('Running: $task');\n"
                "}\n"
            )
        if lang == "lua":
            return f"-- {safe_desc}\nlocal task = {json.dumps(safe_desc)}\nprint('Running: ' .. task)\n"
        if lang == "perl":
            return (
                "use strict;\nuse warnings;\n"
                f"# {safe_desc}\n"
                f"my $task = {json.dumps(safe_desc)};\n"
                "print \"Running: $task\\n\";\n"
            )
        if lang == "r":
            return (
                f"# {safe_desc}\n"
                "run_task <- function(label) {\n"
                "  paste('Running:', label)\n"
                "}\n\n"
                f"cat(run_task({json.dumps(safe_desc)}), '\\n')\n"
            )
        if lang == "julia":
            return (
                f"# {safe_desc}\n"
                "function run_task(label::AbstractString)\n"
                "    return \"Running: $label\"\n"
                "end\n\n"
                f"println(run_task({json.dumps(safe_desc)}))\n"
            )
        if lang == "scala":
            return (
                f"// {safe_desc}\n"
                "object Main {\n"
                "  def main(args: Array[String]): Unit = {\n"
                f"    println({json.dumps('Running: ' + safe_desc)})\n"
                "  }\n"
                "}\n"
            )
        if lang == "haskell":
            return (
                f"-- {safe_desc}\n"
                "module Main where\n\n"
                "main :: IO ()\n"
                "main = do\n"
                f"  putStrLn {json.dumps('Running: ' + safe_desc)}\n"
            )
        if lang == "elixir":
            return (
                f"# {safe_desc}\n"
                "defmodule Main do\n"
                "  def run_task(label) do\n"
                "    IO.puts(\"Running: #{label}\")\n"
                "  end\n"
                "end\n\n"
                f"Main.run_task({json.dumps(safe_desc)})\n"
            )
        if lang in {"jsx", "tsx"}:
            return (
                f"// {safe_desc}\n"
                "import React from 'react';\n\n"
                "export default function App() {\n"
                f"  return <main><h1>{safe_desc}</h1></main>;\n"
                "}\n"
            )
        if lang == "css":
            return (
                f"/* {safe_desc} */\n"
                "* { box-sizing: border-box; }\n"
                "body { margin: 0; font-family: system-ui, sans-serif; }\n"
                ".app { min-height: 100vh; display: grid; place-items: center; }\n"
            )
        if lang == "solidity":
            return (
                "// SPDX-License-Identifier: MIT\n"
                f"// {safe_desc}\n"
                "pragma solidity ^0.8.20;\n\n"
                "contract Main {\n"
                "    string public message = \"Ready\";\n"
                "    function setMessage(string calldata nextMessage) external {\n"
                "        message = nextMessage;\n"
                "    }\n"
                "}\n"
            )
        comment = spec.comment_prefix
        end_comment = " -->" if comment == "<!--" else (" */" if comment == "/*" else "")
        return f"{comment} {spec.display_name}: {safe_desc}{end_comment}\n"


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
