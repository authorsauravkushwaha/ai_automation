import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from hinata.config import OllamaConfig
from hinata.core import ollama_service
from hinata.core.llm_manager import LLMManager, OllamaError
from hinata.core.ollama_service import MODEL_MISSING, NOT_INSTALLED, READY, OllamaService


class StubOllama:
    """Minimal loopback Ollama API with a configurable model list."""

    def __init__(self, models: list[str] | None = None, chat_content: str = "ok") -> None:
        self.models = models if models is not None else ["qwen2.5:0.5b-instruct-q4_K_M"]
        self.chat_content = chat_content
        self.chat_payload: dict = {}
        self.requests: list[str] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                outer.requests.append(self.path)
                outer.respond(self, {"models": [{"name": name} for name in outer.models]})

            def do_POST(self):
                outer.requests.append(self.path)
                outer.chat_payload = json.loads(
                    self.rfile.read(int(self.headers["Content-Length"]))
                )
                outer.respond(self, {"message": {"content": outer.chat_content}})

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @staticmethod
    def respond(handler: BaseHTTPRequestHandler, payload: dict) -> None:
        data = json.dumps(payload).encode()
        handler.send_response(200)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", str(len(data)))
        handler.end_headers()
        handler.wfile.write(data)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_port}"

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()


class OllamaServiceTests(unittest.TestCase):
    def test_running_service_is_reused_without_starting_a_process(self) -> None:
        stub = StubOllama()
        self.addCleanup(stub.close)
        service = OllamaService(OllamaConfig(host=stub.url))
        with patch.object(service, "_spawn_server") as spawn:
            report = service.ensure_running()
        spawn.assert_not_called()
        self.assertEqual(report["state"], READY)
        self.assertTrue(report["model_installed"])
        self.assertFalse(report["started_by_hinata"])

    def test_missing_model_is_reported_separately_from_a_dead_service(self) -> None:
        stub = StubOllama(models=["llama3:8b"])
        self.addCleanup(stub.close)
        service = OllamaService(OllamaConfig(host=stub.url))
        report = service.ensure_running()
        self.assertEqual(report["state"], MODEL_MISSING)
        self.assertTrue(report["service_ready"])
        self.assertFalse(report["model_installed"])
        self.assertIn("hinata model pull", str(report["guidance"]))

    def test_untagged_and_prefixed_model_names_are_accepted(self) -> None:
        service = OllamaService(OllamaConfig())
        self.assertTrue(service._model_present(["qwen2.5:0.5b-instruct-q4_K_M"]))
        self.assertFalse(service._model_present(["qwen2.5:7b-instruct-q4_K_M"]))
        self.assertTrue(OllamaService(OllamaConfig(model="qwen2.5"))._model_present(["qwen2.5:34b"]))

    def test_missing_executable_without_a_listener_is_reported_as_not_installed(self) -> None:
        service = OllamaService(OllamaConfig(host="http://127.0.0.1:9"))
        with patch.object(ollama_service, "find_ollama_executable", return_value=None), patch.object(
            service, "_port_open", return_value=False
        ):
            report = service.ensure_running()
        self.assertEqual(report["state"], NOT_INSTALLED)
        self.assertIn("ollama.com/download", str(report["guidance"]))

    def test_a_dead_port_that_is_already_listening_is_not_called_not_installed(self) -> None:
        # Some managers run Ollama without exposing ollama.exe on PATH.
        service = OllamaService(OllamaConfig(host="http://127.0.0.1:9"))
        with patch.object(ollama_service, "find_ollama_executable", return_value=None), patch.object(
            service, "_port_open", return_value=True
        ):
            report = service.ensure_running()
        self.assertNotEqual(report["state"], NOT_INSTALLED)

    def test_auto_start_disabled_does_not_spawn(self) -> None:
        service = OllamaService(OllamaConfig(host="http://127.0.0.1:9", auto_start=False))
        with patch.object(ollama_service, "find_ollama_executable", return_value="ollama"), patch.object(
            service, "_spawn_server"
        ) as spawn:
            report = service.ensure_running()
        spawn.assert_not_called()
        self.assertEqual(report["state"], "unreachable")
        self.assertFalse(report["service_ready"])

    def test_disabled_configuration_short_circuits(self) -> None:
        service = OllamaService(OllamaConfig(enabled=False))
        report = service.ensure_running()
        self.assertEqual(report["state"], "disabled")


class AutoConnectClientTests(unittest.TestCase):
    def test_chat_connects_to_an_auto_started_service(self) -> None:
        stub = StubOllama()
        self.addCleanup(stub.close)
        config = OllamaConfig(host=stub.url)
        manager = LLMManager(config, OllamaService(config))
        with patch.object(manager.service, "_spawn_server") as spawn:
            self.assertEqual(manager.chat([{"role": "user", "content": "hi"}], "system"), "ok")
        spawn.assert_not_called()
        self.assertIn("/api/chat", stub.requests)

    def test_chat_asks_for_json_history_and_releases_the_model(self) -> None:
        stub = StubOllama(chat_content='{"type":"chat","content":"ok"}')
        self.addCleanup(stub.close)
        manager = LLMManager(OllamaConfig(host=stub.url))
        reply = manager.complete(
            "hello",
            [{"role": "user", "content": "earlier"}],
            [],
            {"system_info": {"required": [], "properties": {}}},
        )
        self.assertEqual(reply, '{"type":"chat","content":"ok"}')
        self.assertEqual(stub.chat_payload["keep_alive"], 0)
        self.assertEqual(stub.chat_payload["format"], "json")
        self.assertEqual(stub.chat_payload["messages"][0]["role"], "system")
        self.assertEqual(stub.chat_payload["messages"][-1]["content"], "hello")

    def test_another_installed_model_is_used_when_the_configured_one_is_missing(self) -> None:
        stub = StubOllama(models=["llama3:8b"])
        self.addCleanup(stub.close)
        manager = LLMManager(OllamaConfig(host=stub.url))
        self.assertEqual(manager.chat([{"role": "user", "content": "hi"}], "system"), "ok")

    def test_no_model_at_all_fails_with_actionable_guidance(self) -> None:
        manager = LLMManager(OllamaConfig())

        def fake_request(endpoint, payload=None, timeout=5):
            return {"models": []}

        with patch.object(manager, "_request", side_effect=fake_request), patch.object(
            manager._service, "ensure_running", return_value={"state": "ready", "service_ready": True}
        ):
            with self.assertRaises(OllamaError) as raised:
                manager.chat([{"role": "user", "content": "hi"}], "system")
        self.assertIn("hinata model pull", str(raised.exception))

    def test_empty_configured_model_uses_the_installed_one(self) -> None:
        stub = StubOllama(models=["qwen2.5:0.5b-instruct-q4_K_M"])
        self.addCleanup(stub.close)
        manager = LLMManager(OllamaConfig(host=stub.url, model=""))
        self.assertEqual(manager.active_model(), "qwen2.5:0.5b-instruct-q4_K_M")

    def test_status_reports_a_started_but_model_less_runtime(self) -> None:
        stub = StubOllama(models=[])
        self.addCleanup(stub.close)
        manager = LLMManager(OllamaConfig(host=stub.url, model=""))
        status = manager.status()
        self.assertTrue(status["available"])
        self.assertFalse(status["installed"])


if __name__ == "__main__":
    unittest.main()
