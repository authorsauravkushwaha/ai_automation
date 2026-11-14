import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LAUNCHER = ROOT / "scripts" / "offline_linux.py"


def namespace_available():
    if not sys.platform.startswith("linux") or not shutil.which("unshare") or not shutil.which("ip"):
        return False
    try:
        return subprocess.run([sys.executable, str(LAUNCHER), "--check"], capture_output=True, timeout=15).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


@unittest.skipUnless(namespace_available(), "Linux user/network namespaces or ip unavailable")
class OfflineLinuxTests(unittest.TestCase):
    def test_real_namespace_probe_and_deterministic_request(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = os.environ.copy()
            env["HINATA_DATA_DIR"] = str(Path(tmp) / "data")
            proc = subprocess.run([sys.executable, str(LAUNCHER), "--", "--no-llm", "ask", "system info"],
                                  cwd=ROOT, env=env, capture_output=True, text=True, timeout=20)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("external IPv4/IPv6 probe unroutable", proc.stdout)
        self.assertIn("logical_cpus", proc.stdout)

    def test_host_loopback_service_is_not_reachable(self):
        received = []
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                received.append(self.path)
                self.send_response(200)
                self.end_headers()
            def log_message(self, *args):
                pass
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                cfg = Path(tmp) / "config.toml"
                cfg.write_text(f'[ollama]\nhost = "http://127.0.0.1:{server.server_port}"\nauto_start = false\nauto_pull = false\n', encoding="utf-8")
                env = os.environ.copy()
                env["HINATA_DATA_DIR"] = str(Path(tmp) / "data")
                proc = subprocess.run([sys.executable, str(LAUNCHER), "--config", str(cfg), "--", "model", "status"],
                                      cwd=ROOT, env=env, capture_output=True, text=True, timeout=20)
                self.assertIn("cannot reach local ollama", proc.stdout.lower())
                self.assertEqual(received, [])
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_offline_workflow_with_simulated_model_and_persistent_data(self):
        # This uses a protocol stub, not actual LLM, embedding or audio models.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            docs = root / "documents"
            docs.mkdir()
            (docs / "meeting.txt").write_text("The local meeting is Tuesday.", encoding="utf-8")
            bin_dir = root / "bin"
            bin_dir.mkdir()
            ollama = bin_dir / "ollama"
            ollama.symlink_to(ROOT / "tests/fixtures/offline_ollama_stub.py")
            # The executable bit is set on the fixture in the repository.
            cfg = root / "config.toml"
            cfg.write_text(f'''[ollama]
host = "http://127.0.0.1:11434"
model = "chat:latest"
embedding_model = "embed"
auto_start = false
auto_pull = false
[security]
allowed_roots = ["{docs}"]
''', encoding="utf-8")
            env = os.environ.copy()
            env["PATH"] = str(bin_dir) + os.pathsep + env["PATH"]
            env["HINATA_DATA_DIR"] = str(root / "data")
            def ask(text, input_text=""):
                return subprocess.run([sys.executable, str(LAUNCHER), "--config", str(cfg), "--with-ollama", "--", "ask", text],
                                      cwd=ROOT, env=env, input=input_text, capture_output=True, text=True, timeout=20)
            chat = ask("hello")
            self.assertEqual(chat.returncode, 0, chat.stdout + chat.stderr)
            self.assertIn("local test server", chat.stdout)
            imported = ask("semantic index document meeting.txt", "yes\n")
            self.assertEqual(imported.returncode, 0, imported.stdout + imported.stderr)
            answer = ask("ask knowledge when is the local meeting?")
            self.assertEqual(answer.returncode, 0, answer.stdout + answer.stderr)
            self.assertIn("Tuesday [1]", answer.stdout)
            self.assertIn("meeting.txt chunk 1", answer.stdout)
            saved = ask("remember that my editor is vim", "yes\n")
            self.assertEqual(saved.returncode, 0, saved.stdout + saved.stderr)
            recalled = ask("list preferences")
            self.assertIn("editor: vim", recalled.stdout)
            folder = ask("create folder offline-project")
            self.assertEqual(folder.returncode, 0, folder.stdout + folder.stderr)
            self.assertTrue((docs / "offline-project").is_dir())

    def test_forwarded_config_cannot_bypass_preflight(self):
        proc = subprocess.run([sys.executable, str(LAUNCHER), "--", "--config=other.toml", "ask", "system info"],
                              cwd=ROOT, capture_output=True, text=True, timeout=20)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("Pass --config to the offline launcher", proc.stderr)

    def test_auto_pull_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "config.toml"
            cfg.write_text('[ollama]\nauto_pull = true\n', encoding="utf-8")
            proc = subprocess.run([sys.executable, str(LAUNCHER), "--config", str(cfg), "--", "--no-llm", "ask", "system info"],
                                  cwd=ROOT, capture_output=True, text=True, timeout=20)
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("Disable ollama.auto_pull", proc.stderr)


if __name__ == "__main__":
    unittest.main()
