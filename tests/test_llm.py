import threading
import unittest
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

        with patch("urllib.request.urlopen", return_value=response):
            worker = threading.Thread(target=request)
            worker.start()
            self.assertTrue(response.read_started.wait(timeout=1))
            manager.cancel_active_request()
            worker.join(timeout=1)

        self.assertFalse(worker.is_alive())
        self.assertTrue(response.closed)
        self.assertTrue(errors)
        self.assertIsInstance(errors[0], OllamaError)

    def test_default_model_is_small_local_qwen_profile(self) -> None:
        config = OllamaConfig()
        self.assertEqual(config.model, "qwen2.5:0.5b-instruct-q4_K_M")
        self.assertEqual(config.threads, 2)
        self.assertEqual(config.context_tokens, 2048)


if __name__ == "__main__":
    unittest.main()
