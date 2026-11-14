"""Opt-in acceptance test using installed models, never downloads or stubs them.

Requires Linux namespaces, Ollama and two explicitly preloaded models. The test
uses temporary data and restarts the isolated service between CLI commands.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LAUNCHER = ROOT / "scripts" / "offline_linux.py"


@unittest.skipUnless(os.environ.get("HINATA_REAL_OFFLINE_ACCEPTANCE") == "1", "real offline acceptance is opt-in")
class RealOfflineAcceptance(unittest.TestCase):
    def test_isolated_preloaded_models_and_persistent_workflow(self):
        chat = os.environ.get("HINATA_ACCEPTANCE_CHAT_MODEL", "")
        embed = os.environ.get("HINATA_ACCEPTANCE_EMBED_MODEL", "")
        self.assertTrue(chat and embed, "Set HINATA_ACCEPTANCE_CHAT_MODEL and HINATA_ACCEPTANCE_EMBED_MODEL")
        self.assertTrue(shutil.which("ollama"), "Install Ollama and prepare models before going offline")
        with tempfile.TemporaryDirectory(prefix="hinata-acceptance-") as tmp:
            root = Path(tmp)
            docs = root / "documents"
            docs.mkdir()
            (docs / "meeting.txt").write_text("The project meeting will be held Tuesday in room birch.", encoding="utf-8")
            cfg = root / "config.toml"
            cfg.write_text(
                f'data_dir = {json.dumps(str(root / "data"))}\n'
                f'[ollama]\nmodel = {json.dumps(chat)}\nembedding_model = {json.dumps(embed)}\n'
                'host = "http://127.0.0.1:11434"\nauto_start = false\nauto_pull = false\ntimeout_seconds = 600\n'
                f'[security]\nallowed_roots = [{json.dumps(str(docs))}]\n', encoding="utf-8")

            def run(*args, input_text=""):
                proc = subprocess.run([sys.executable, str(LAUNCHER), "--config", str(cfg), "--with-ollama", "--", *args],
                                      cwd=ROOT, input=input_text, capture_output=True, text=True, timeout=720)
                self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
                self.assertIn("external IPv4/IPv6 probe unroutable", proc.stdout)
                return proc.stdout

            status = run("model", "status")
            report = json.loads(status[status.index("{"):])
            self.assertTrue(report["tags"]["installed"], "Configured chat model is not installed")
            self.assertIn(embed if ":" in embed else embed + ":latest", report["tags"]["models"],
                          "Configured embedding model is not installed")
            # A free-form request goes through the actual model, not a deterministic tool.
            run("ask", "Briefly explain what a calendar is.")
            self.assertIn("Indexed", run("ask", "semantic index document meeting.txt", input_text="yes\n"))
            self.assertIn("meeting.txt", run("ask", "semantic search for project meeting"))
            run("ask", "remember that my editor is vim", input_text="yes\n")
            self.assertIn("editor: vim", run("ask", "list preferences"))
            run("ask", "create folder offline-project")
            self.assertTrue((docs / "offline-project").is_dir())


if __name__ == "__main__":
    unittest.main()
