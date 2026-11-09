import os
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from hinata.config import OllamaConfig
from hinata.core.llm_manager import LLMManager, OllamaError


class BlockingResponse:
    def __init__(self) -> None:
        self.read_started = threading.Event()
        self.release = threading.Event()
        self.closed = False

    def __enter__(self) -> "BlockingResponse":
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def read(self, _limit: int) -> bytes:
        self.read_started.set()
        self.release.wait(timeout=2)
        if self.closed:
            raise ValueError("response was closed")
        return b'{"models": []}'

    def close(self) -> None:
        self.closed = True
        self.release.set()


class LLMTests(unittest.TestCase):
    def test_cancel_closes_active_local_response(self) -> None:
        manager = LLMManager(OllamaConfig())
        response = BlockingResponse()
        errors: list[Exception] = []

        def request() -> None:
            try:
                manager._request("/api/tags")
            except Exception as exc:
                errors.append(exc)

        with patch.object(manager._opener, "open", return_value=response):
            worker = threading.Thread(target=request)
            worker.start()
            self.assertTrue(response.read_started.wait(timeout=1))
            manager.cancel_active_request()
            worker.join(timeout=1)

        self.assertFalse(worker.is_alive())
        self.assertTrue(response.closed)
        self.assertTrue(errors)
        self.assertIsInstance(errors[0], OllamaError)

    def test_loopback_redirects_never_forward_prompts_and_proxies_are_ignored(self) -> None:
        requests = []

        class RedirectHandler(BaseHTTPRequestHandler):
            def do_GET(self):
                requests.append(self.path)
                self.send_response(302)
                self.send_header("Location", f"http://127.0.0.1:{self.server.server_port}/leak")
                self.end_headers()

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), RedirectHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            config = OllamaConfig(host=f"http://127.0.0.1:{server.server_port}")
            with patch.dict(os.environ, {"http_proxy": "http://127.0.0.1:1", "no_proxy": ""}):
                manager = LLMManager(config)
                with self.assertRaises(OllamaError):
                    manager._request("/api/tags")
            self.assertEqual(requests, ["/api/tags"])
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_chat_releases_model_after_inference(self) -> None:
        manager = LLMManager(OllamaConfig())
        with patch.object(manager, "_request", return_value={"message": {"content": "ok"}}) as request:
            self.assertEqual(manager.chat([], "system"), "ok")
        self.assertEqual(request.call_args.args[1]["keep_alive"], 0)

    def test_chat_requests_decode_time_json_from_ollama(self) -> None:
        manager = LLMManager(OllamaConfig())
        with patch.object(manager, "_request", return_value={"message": {"content": "ok"}}) as request:
            manager.chat([], "system")
        self.assertEqual(request.call_args.args[1]["format"], "json")

    def test_default_model_is_small_local_qwen_profile(self) -> None:
        config = OllamaConfig()
        self.assertEqual(config.model, "qwen2.5:0.5b-instruct-q4_K_M")
        self.assertEqual(config.threads, 2)
        self.assertEqual(config.context_tokens, 2048)


if __name__ == "__main__":
    unittest.main()
