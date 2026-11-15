"""No-network regression tests for consented local fleet and PNG image setup."""

from __future__ import annotations

import base64
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from hinata.automation.app_control import AutomationError
from hinata.automation.file_manager import FileManager
from hinata.automation.workspace import WorkspaceManager
from hinata.config import ConfigError, SecurityConfig, load_config
from hinata.core.fleet_setup import apply_fleet_pack
from hinata.core.jarvis_models import FLEET_CATALOG, ProjectArchitectEngine, VisualStudioEngine
from hinata.core.local_image import (
    download_image_model,
    is_model_ready,
    model_directory,
    render_offline_png,
)
from hinata.main import main

ROOT = Path(__file__).resolve().parent.parent
# Small valid PNG for testing worker *plumbing*, not testing diffusion quality.
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Y+d0RQAAAAASUVORK5CYII="
)
MODEL_FILES = (
    "model_index.json", "unet/config.json", "unet/diffusion_pytorch_model.safetensors",
    "vae/config.json", "text_encoder/config.json", "text_encoder/model.safetensors",
    "tokenizer/vocab.json", "tokenizer/merges.txt",
)


class LocalImageSetupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.model_dir = model_directory(self.root)

    def make_cached_weights(self) -> None:
        for item in MODEL_FILES:
            file = self.model_dir / item
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_bytes(b"cached test fixture")

    def test_setup_download_invoked_only_explicitly(self) -> None:
        self.assertFalse(is_model_ready(self.model_dir))
        with patch.dict("sys.modules", {"huggingface_hub": None}):
            with self.assertRaises(AutomationError):
                download_image_model(self.root)
        self.assertFalse(is_model_ready(self.model_dir))
        self.make_cached_weights()
        self.assertTrue(is_model_ready(self.model_dir))
        (self.model_dir / "tokenizer/merges.txt").unlink()
        self.assertFalse(is_model_ready(self.model_dir))

    def test_real_png_backend_is_used_only_if_weights_cached(self) -> None:
        files = FileManager(SecurityConfig(allowed_roots=[self.root]))
        studio = VisualStudioEngine(
            files, image_backend_url="", image_model_dir=self.model_dir
        )
        with patch("hinata.core.local_image.render_offline_png") as render:
            svg = studio.generate_image("garden", "first.png", llm_enabled=False)
            self.assertEqual(svg["format"], "svg")
            render.assert_not_called()
        self.make_cached_weights()

        def fake_render(_model: Path, target: Path, _prompt: str, _w: int, _h: int) -> bytes:
            target.write_bytes(PNG)
            return PNG

        with patch("hinata.core.local_image.render_offline_png", side_effect=fake_render) as render:
            png = studio.generate_image("garden", "second.png", width=1024, llm_enabled=False)
            self.assertEqual(png["format"], "png")
            self.assertEqual(png["backend"], "offline_sd_turbo")
            self.assertEqual(png["width"], 768)
            self.assertEqual(render.call_args.args[3], 768)
        self.assertEqual((self.root / "second.png").read_bytes(), PNG)

    def test_worker_is_offline_and_cannot_overwrite_target(self) -> None:
        self.make_cached_weights()

        def fake_worker(*_args: object, **kwargs: object) -> object:
            command = kwargs["args"] if "args" in kwargs else _args[0]
            # The private worker gets a temporary path, never the final owner file.
            Path(command[5]).write_bytes(PNG)  # type: ignore[index]
            class Result:
                returncode = 0
                stderr = ""
            return Result()

        with patch("hinata.core.local_image.subprocess.run", side_effect=fake_worker) as run:
            target = self.root / "result.png"
            self.assertEqual(render_offline_png(self.model_dir, target, "garden", 512, 512), PNG)
            env = run.call_args.kwargs["env"]
            self.assertEqual(env["HF_HUB_OFFLINE"], "1")
            self.assertEqual(env["TRANSFORMERS_OFFLINE"], "1")
            self.assertEqual(env["HTTPS_PROXY"], "")
            with self.assertRaises(AutomationError):
                render_offline_png(self.model_dir, target, "garden", 512, 512)
        self.assertEqual(target.read_bytes(), PNG)

    def test_reasoner_and_coder_cooperate_before_any_file_write(self) -> None:
        llm = Mock()
        llm.chat.side_effect = [
            "Implement a bounded CLI, then add two tests.",
            json.dumps({"files": {"README.md": "Readme", "todo.py": "def main():\n    return 0\n"}}),
        ]
        llm.last_model_identity = "qwen2.5-coder:1.5b"
        files = FileManager(SecurityConfig(allowed_roots=[self.root]))
        project = ProjectArchitectEngine(files, WorkspaceManager(Mock(), [self.root]), llm)
        generated, generator = project._synthesize_files(
            project_name="todo", specification="Build a todo CLI", template="python_cli",
            llm_enabled=True,
        )
        self.assertEqual(llm.chat.call_args_list[0].kwargs["role"], "planner")
        self.assertEqual(llm.chat.call_args_list[1].kwargs["role"], "coder")
        self.assertIn("implementation_plan", llm.chat.call_args_list[1].args[0][0]["content"])
        self.assertEqual(generated["todo.py"], "def main():\n    return 0\n")
        self.assertEqual(generator, "local_coder:qwen2.5-coder:1.5b")

    def test_setup_consent_is_separate_and_precedes_downloads(self) -> None:
        script = (ROOT / "setup.ps1").read_bytes().decode("ascii")
        self.assertIn("function Confirm-OptionalDownload", script)
        self.assertIn("[Console]::IsInputRedirected", script)
        fleet_prompt = script.index('Confirm-OptionalDownload "Download and activate this multi-AI fleet')
        self.assertLess(fleet_prompt, script.index('Invoke-Native $OllamaPath @("pull", $Name)'))
        self.assertLess(script.index('Invoke-Native $OllamaPath @("pull", $Name)'),
                        script.index('Invoke-Native $VenvPython @("-m", "hinata", "model", "apply-fleet"'))
        self.assertLess(script.index('Confirm-OptionalDownload "Install image packages'),
                        script.index('Invoke-Native $VenvPython @("-m", "pip", "install", "-e", ".[image]")'))
        self.assertIn("-SkipModelDownload", script)


class FleetApplyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.cfg = self.root / "config.toml"
        self.cfg.write_text((ROOT / "config.example.toml").read_text(encoding="utf-8"))

    def test_apply_preserves_security_and_creates_non_overwriting_backups(self) -> None:
        before = self.cfg.read_text()
        pack = FLEET_CATALOG["lenovo_loq_i5_cpu_fast"]
        backup = apply_fleet_pack(self.cfg, pack)
        self.assertEqual(backup.read_text(), before)
        updated = self.cfg.read_text()
        self.assertIn("[security]", updated)
        self.assertIn('strict_offline = false', updated)
        self.assertIn('model = "qwen2.5:1.5b"', updated)
        self.assertEqual(load_config(self.cfg).ollama.vision_model, "moondream")
        second_backup = apply_fleet_pack(self.cfg, pack)
        self.assertNotEqual(backup, second_backup)
        self.assertEqual(backup.read_text(), before)
        self.assertEqual(self.cfg.read_text(), updated)

    def test_invalid_existing_config_is_not_overwritten(self) -> None:
        invalid = self.cfg.read_text().replace('hardware_profile = "auto"',
                                               'hardware_profile = "auto"\nunknown_ollama_key = true')
        self.cfg.write_text(invalid)
        with self.assertRaises(ConfigError):
            apply_fleet_pack(self.cfg, FLEET_CATALOG["lenovo_loq_i5_cpu_fast"])
        self.assertEqual(self.cfg.read_text(), invalid)
        self.assertFalse(self.cfg.with_suffix(".toml.bak").exists())

    def test_cli_requires_explicit_pack_and_only_updates_selected_config(self) -> None:
        before = self.cfg.read_bytes()
        with patch("builtins.print"):
            self.assertEqual(main(["--config", str(self.cfg), "model", "apply-fleet"]), 2)
        self.assertEqual(self.cfg.read_bytes(), before)
        with patch("builtins.print"):
            self.assertEqual(main(["--config", str(self.cfg), "model", "apply-fleet",
                                   "--pack", "lenovo_loq_i5_cpu_fast"]), 0)
        self.assertEqual(load_config(self.cfg).ollama.hardware_profile, "lenovo_loq_i5")
        self.assertTrue(self.cfg.with_suffix(".toml.bak").exists())


if __name__ == "__main__":
    unittest.main()
