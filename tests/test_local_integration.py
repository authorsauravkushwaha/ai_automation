"""Exercise the real local HTTP client and assistant without a downloaded model."""

import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from tests.helpers import make_assistant


class LocalIntegrationTests(unittest.TestCase):
    def test_text_request_to_loopback_model_and_persistent_memory(self) -> None:
        received = []

        class LocalModelStub(BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"]))
                received.append((self.path, json.loads(body)))
                data = json.dumps({"message": {"content": '{"type":"chat","content":"Local response"}'}}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), LocalModelStub)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as directory:
                assistant = make_assistant(directory, llm_enabled=True)
                try:
                    assistant.llm.base_url = f"http://127.0.0.1:{server.server_port}"
                    response = assistant.process_text("Explain this local integration test")
                    self.assertTrue(response.success, response.text)
                    self.assertEqual(response.text, "Local response")
                    history = assistant.memory.recent_messages()
                    self.assertEqual(history[-1]["content"], "Local response")
                    self.assertEqual(received[0][0], "/api/chat")
                    self.assertEqual(received[0][1]["keep_alive"], assistant.config.ollama.keep_alive)
                    self.assertEqual(received[0][1]["messages"][-1]["role"], "user")
                finally:
                    assistant.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join()
