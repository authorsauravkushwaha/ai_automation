#!/usr/bin/env python3
"""Small fake Ollama protocol server for offline integration tests, not a model."""
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def reply(self, obj):
        body = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self.reply({"models": [{"name": "chat:latest", "digest": "chat-digest"},
                               {"name": "embed:latest", "digest": "embed-digest"}]})

    def do_POST(self):
        data = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if self.path == "/api/embed":
            self.reply({"embeddings": [[1.0, 0.0] for _ in data["input"]]})
        elif self.path == "/api/chat":
            system = data["messages"][0]["content"]
            answer = "The local meeting is Tuesday [1]." if system.startswith("Answer the user's") else '{"type":"chat","content":"Hello from the local test server."}'
            self.reply({"message": {"content": answer}})
        else:
            self.send_error(404)


if __name__ == "__main__":
    port = int(os.environ.get("OLLAMA_HOST", "127.0.0.1:11434").rsplit(":", 1)[1])
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
