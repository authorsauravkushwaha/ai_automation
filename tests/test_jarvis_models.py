"""Lenovo LOQ fleet, offline image studio and approved project builder regressions."""

from __future__ import annotations

import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from hinata.agents.workers import WorkerCoordinator
from hinata.automation.app_control import AutomationError
from hinata.automation.file_manager import FileManager
from hinata.automation.workspace import WorkspaceManager
from hinata.config import ConfigError, OllamaConfig, SecurityConfig
from hinata.core.intent_parser import IntentParser
from hinata.core.jarvis_models import (
    FLEET_CATALOG,
    ProjectArchitectEngine,
    VisualStudioEngine,
    classify_task_model_role,
    render_fleet_toml_snippet,
    select_optimal_fleet_pack,
    strip_reasoning_traces,
)
from hinata.core.llm_manager import LLMManager
from hinata.core.model_reply import extract_reply_object, plain_text_reply
from hinata.core.ollama_service import OllamaService
from hinata.core.permissions import POLICY, PermissionLevel
from hinata.database.database import Database
from hinata.security.action_validator import ActionValidator


class JarvisFleetTests(unittest.TestCase):
    def test_hardware_tiers_and_config(self) -> None:
        for vram, ram, expected in [
            (8192, 16, "lenovo_loq_i5_rtx_8gb"),
            (4096, 12, "lenovo_loq_i5_rtx_4gb"),
            (0, 8, "lenovo_loq_i5_cpu_fast"),
        ]:
            snapshot = {"memory": {"total_gb": ram}, "gpu": {"vram_mb": vram}}
            pack = select_optimal_fleet_pack(snapshot)
            self.assertEqual(pack.pack_id, expected)
            self.assertIn("deepseek", pack.planner_model)
            self.assertIn("qwen", pack.chat_model)
            self.assertEqual(len(pack.all_ollama_models()), len(set(pack.all_ollama_models())))
            cfg = tomllib.loads(render_fleet_toml_snippet(pack))["ollama"]
            OllamaConfig(**cfg).validate()
        battery = {"memory": {"total_gb": 32}, "gpu": {"vram_mb": 8192}, "power": {"on_battery": True}}
        self.assertEqual(select_optimal_fleet_pack(battery).pack_id, "lenovo_loq_i5_cpu_fast")
        self.assertEqual(len(FLEET_CATALOG), 3)

    def test_lenovo_profile_activates_role_pins_and_respects_owner_overrides(self) -> None:
        snapshot = {"memory": {"total_gb": 16}, "gpu": {"vram_mb": 6144}}
        with patch("hinata.core.providers.HardwareCapabilityService.detect", return_value=snapshot):
            cfg = OllamaConfig(hardware_profile="lenovo_loq_i5", coding_model="my-private-coder:latest")
            manager = LLMManager(cfg)
        self.assertEqual(manager.router.model_for_role("chat"), FLEET_CATALOG["lenovo_loq_i5_rtx_8gb"].chat_model)
        self.assertEqual(manager.router.model_for_role("planner"), "deepseek-r1:7b")
        self.assertEqual(manager.router.model_for_role("coder"), "my-private-coder:latest")
        self.assertEqual(manager.router.model_for_role("vision"), "qwen2.5vl:7b")

    def test_ollama_service_caps_resident_models(self) -> None:
        service = OllamaService(OllamaConfig())
        with patch("hinata.core.ollama_service.subprocess.Popen") as popen:
            self.assertTrue(service._spawn_server("ollama"))
        environment = popen.call_args.kwargs["env"]
        self.assertEqual(environment["OLLAMA_HOST"], "127.0.0.1:11434")
        self.assertEqual(environment["OLLAMA_MAX_LOADED_MODELS"], "1")
        self.assertEqual(environment["OLLAMA_FLASH_ATTENTION"], "1")

    def test_routing_permissions_and_reasoning_filter(self) -> None:
        self.assertEqual(classify_task_model_role("fix this Python bug"), "coder")
        self.assertEqual(classify_task_model_role("design a logo"), "vision")
        self.assertEqual(classify_task_model_role("plan an automation workflow"), "planner")
        self.assertEqual(classify_task_model_role("hello"), "chat")
        self.assertEqual(POLICY["ai_fleet_profile"], PermissionLevel.SAFE)
        self.assertEqual(POLICY["generate_image"], PermissionLevel.CONFIRM)
        self.assertEqual(POLICY["build_project"], PermissionLevel.CONFIRM)
        parser = IntentParser(ActionValidator())
        self.assertEqual(parser.parse("ai fleet").tool, "ai_fleet_profile")  # type: ignore[union-attr]
        self.assertEqual(parser.parse("generate image blue moon to drawing.svg").tool, "generate_image")  # type: ignore[union-attr]
        self.assertEqual(parser.parse("build project a todo CLI in myapp").tool, "build_project")  # type: ignore[union-attr]
        self.assertEqual(extract_reply_object('<think>{"type":"tool","tool":"bad"}</think>{"type":"chat","content":"ready"}'), {"type": "chat", "content": "ready"})
        self.assertEqual(plain_text_reply("<think>private chain</think>Done."), "Done.")
        self.assertIsNone(extract_reply_object('<think>{"type":"tool","tool":"bad"}'))
        self.assertIsNone(plain_text_reply('<think>{"type":"tool","tool":"bad"}'))
        self.assertEqual(strip_reasoning_traces('<think>{"type":"tool"}'), "")
        self.assertEqual(WorkerCoordinator.__name__, "WorkerCoordinator")

    def test_loopback_config_rejects_remote_image_server(self) -> None:
        for url in ("https://127.0.0.1:7860", "http://example.com", "http://127.0.0.1:7860@evil.com"):
            with self.assertRaises(ConfigError):
                OllamaConfig(image_backend_url=url).validate()


class JarvisStudioTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.files = FileManager(SecurityConfig(allowed_roots=[self.root]))

    def test_offline_svg_fallback_and_no_overwrite(self) -> None:
        studio = VisualStudioEngine(self.files, image_backend_url="")
        result = studio.generate_image("Blue moon & stars", "moon.svg", llm_enabled=False)
        self.assertEqual(result["format"], "svg")
        self.assertIn("Blue moon &amp; stars", Path(result["path"]).read_text())
        with self.assertRaises(AutomationError):
            studio.generate_image("another", "moon.svg", llm_enabled=False)
        with self.assertRaises(AutomationError):
            studio.generate_image("another", "../escape.svg", llm_enabled=False)
        with self.assertRaises(AutomationError):
            studio.generate_image("another", "file.exe", llm_enabled=False)

    def test_rejects_executable_svg(self) -> None:
        for svg in (
            '<svg xmlns="http://www.w3.org/2000/svg"><foreignObject>hello</foreignObject></svg>',
            '<svg onload="alert(1)" />',
            '<svg><image href="https://example.com/evil" /></svg>',
            '<!DOCTYPE svg [<!ENTITY x SYSTEM "file:///etc/passwd">]><svg>&x;</svg>',
        ):
            with self.assertRaises(ValueError):
                VisualStudioEngine._sanitize_svg(svg)

    def test_project_builder_sandbox_and_escape_protection(self) -> None:
        workspace = WorkspaceManager(Mock(), [self.root])
        architect = ProjectArchitectEngine(self.files, workspace)
        architect._synthesize_files = Mock(return_value=({"../escape.py": "print('x')"}, "mock"))  # type: ignore[method-assign]
        with self.assertRaises(AutomationError):
            architect.build_project(project_dir="safe", specification="Make a CLI", llm_enabled=False)
        self.assertFalse((self.root / "escape.py").exists())
        architect._synthesize_files = Mock(return_value=({"ok.py": "x = 1", "bad.py": "def broken("}, "mock"))  # type: ignore[method-assign]
        with self.assertRaises(AutomationError):
            architect.build_project(project_dir="safe", specification="Make a CLI", llm_enabled=False)
        self.assertFalse((self.root / "safe" / "ok.py").exists())
        (self.root / "occupied").mkdir()
        (self.root / "occupied" / "README.md").write_text("original")
        with self.assertRaises(AutomationError):
            architect.build_project(project_dir="occupied", specification="Make a CLI", llm_enabled=False)
        self.assertEqual((self.root / "occupied" / "README.md").read_text(), "original")

    def test_real_scaffold_runs_tests_in_offline_sandbox(self) -> None:
        database = Database(self.root / "data.db")
        database.ensure_migrated()
        workspace = WorkspaceManager(database, [self.root])
        project = ProjectArchitectEngine(self.files, workspace).build_project(
            project_dir="todo_app", specification="Create a simple todo tool",
            llm_enabled=False,
        )
        self.assertTrue(project["sandbox_verification"]["tests_passed"])
        self.assertEqual(project["status"], "PARTIAL")
        self.assertIn("Ran 2 tests", project["sandbox_verification"]["stderr"])
        self.assertTrue((self.root / "todo_app" / "tests" / "test_core.py").is_file())
        canonical_root = str(Path(project["project_root"]).resolve())
        self.assertEqual(
            database.fetch_one(
                "SELECT status FROM project_records WHERE root_path = ?", (canonical_root,)
            )["status"],
            "PARTIAL",
        )
        with self.assertRaises(AutomationError):
            ProjectArchitectEngine(self.files, workspace).build_project(
                project_dir="todo_app", specification="overwrite", llm_enabled=False,
            )

    def test_missing_or_failing_verification_never_reports_success(self) -> None:
        database = Database(self.root / "lifecycle.db")
        database.ensure_migrated()
        workspace = WorkspaceManager(database, [self.root])
        architect = ProjectArchitectEngine(self.files, workspace)
        architect._synthesize_files = Mock(return_value=({"README.md": "requirements pending"}, "test-generator"))  # type: ignore[method-assign]
        partial = architect.build_project(project_dir="unverified", specification="A product", llm_enabled=False)
        self.assertEqual(partial["status"], "PARTIAL")
        self.assertFalse(partial["sandbox_verification"]["executed"])
        architect._synthesize_files = Mock(
            return_value=(
                {
                    "tests/test_failure.py": (
                        "import unittest\n\n"
                        "class FailureTests(unittest.TestCase):\n"
                        "    def test_failure(self):\n"
                        "        self.fail('controlled defect')\n"
                    )
                },
                "test-generator",
            )
        )  # type: ignore[method-assign]
        failed = architect.build_project(project_dir="failing", specification="A broken product", llm_enabled=False)
        self.assertEqual(failed["status"], "FAILED")
        self.assertFalse(failed["sandbox_verification"]["tests_passed"])
        self.assertIn("FAILED", {check["status"] for check in failed["sandbox_verification"]["checks"]})


if __name__ == "__main__":
    unittest.main()
