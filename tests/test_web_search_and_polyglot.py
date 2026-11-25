"""Regression tests for HINATA live search, safe fetching, and polyglot code generation."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from hinata.agents.coder import PolyglotCoder, resolve_language
from hinata.automation.app_control import AppControl, AutomationError
from hinata.automation.file_manager import FileManager
from hinata.automation.smart_scraper import PriceParser, TableParser
from hinata.automation.web_search import (
    WebSearchEngine,
    _DuckDuckGoHTMLParser,
    extract_core_search_query,
    is_safe_public_url,
)
from hinata.config import SecurityConfig
from hinata.core.intent_parser import IntentParser
from hinata.core.model_reply import plain_text_reply
from hinata.security.action_validator import ActionValidator
from tests.helpers import make_assistant


class WebSearchAndPolyglotTests(unittest.TestCase):
    def test_search_topic_extraction_retains_author_name_and_drops_request_tail(self):
        query = "Search about author Saurav Kushwaha, from scratch to last. Tell me his every work from start to end"
        self.assertEqual(extract_core_search_query(query), "author Saurav Kushwaha")

    def test_model_reply_salvages_topic_and_truncated_chat_but_not_actions_or_blank(self):
        self.assertEqual(plain_text_reply('{"topic":"Saurav Kushwaha","summary":"Author"}'), "Topic: Saurav Kushwaha\nSummary: Author")
        self.assertEqual(plain_text_reply('{"type":"chat","content":"A useful partial reply'), "A useful partial reply")
        self.assertIsNone(plain_text_reply('{"type":"tool","tool":"open_application"'))
        self.assertIsNone(plain_text_reply('{"type":"chat","content":""}'))

    def test_ssrf_filter_rejects_private_and_non_http_urls(self):
        for url in (
            "http://127.0.0.1:11434/api/tags",
            "http://10.0.0.1/admin",
            "http://169.254.169.254/latest/meta-data",
            "file:///etc/passwd",
            "http://localhost/",
        ):
            self.assertFalse(is_safe_public_url(url), url)

    def test_search_is_fail_closed_when_disabled_or_strict_offline(self):
        with self.assertRaisesRegex(Exception, "disabled"):
            WebSearchEngine(enabled=False).search("HINATA")
        with self.assertRaisesRegex(Exception, "strict_offline"):
            WebSearchEngine(strict_offline=True).search("HINATA")

    def test_ssrf_filter_fails_closed_when_public_host_does_not_resolve(self):
        with patch("hinata.automation.web_search.socket.getaddrinfo", side_effect=OSError("no DNS")):
            self.assertFalse(is_safe_public_url("https://example.com/"))

    def test_fetch_rejects_private_url_before_network_request(self):
        engine = WebSearchEngine()
        with patch.object(engine._opener, "open") as http:
            with self.assertRaises(AutomationError):
                engine.fetch_webpage("http://127.0.0.1/private")
            http.assert_not_called()

    def test_search_and_app_commands_parse_deterministically(self):
        parser = IntentParser(ActionValidator())
        self.assertEqual(parser.parse("Search about author Saurav Kushwaha").tool, "web_search")
        self.assertEqual(parser.parse("search internet about HINATA").tool, "web_search")
        self.assertEqual(parser.parse("google HINATA assistant").tool, "web_search")
        self.assertEqual(parser.parse("Who is Ada Lovelace?").tool, "web_search")
        self.assertEqual(parser.parse("tell me about recent advances in robotics").tool, "web_search")
        self.assertEqual(parser.parse("fetch webpage https://example.com").tool, "fetch_webpage")
        self.assertEqual(parser.parse("list installed apps").tool, "list_installed_apps")
        self.assertEqual(
            parser.parse_multitask("search internet about HINATA and open notepad"),
            ["search internet about HINATA", "open notepad"],
        )

    def test_polyglot_language_aliases_cover_common_languages(self):
        self.assertEqual(resolve_language("C++").extension, ".cpp")
        self.assertEqual(resolve_language("ts").canonical, "typescript")
        self.assertEqual(resolve_language("PowerShell").extension, ".ps1")
        self.assertEqual(resolve_language("Kotlin").extension, ".kt")

    def test_offline_fallback_writes_clean_sources_inside_allowed_roots(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            files = FileManager(SecurityConfig(allowed_roots=[root]))
            coder = PolyglotCoder(files)
            result = coder.generate_code(
                task="Create a tiny HTTP server",
                language="go",
                output_path=str(root / "server.go"),
                llm_enabled=False,
            )
            self.assertIn("package main", result["code"])
            self.assertEqual(Path(result["saved_path"]).read_text(), result["code"])
            self.assertGreater(result["bytes_written"], 0)

    def test_malformed_model_plan_retries_as_safe_display_only_chat(self):
        with tempfile.TemporaryDirectory() as directory:
            assistant = make_assistant(directory, llm_enabled=True)
            try:
                assistant.llm.complete = lambda *args, **kwargs: "{\"type\": \"chat\", \"content\": "
                assistant.llm.chat = lambda messages, system, **kwargs: "Here is a plain-language answer."
                result = assistant.process_text("Explain how photosynthesis works")
                self.assertTrue(result.success, result.text)
                self.assertEqual(result.status, "completed")
                self.assertEqual(result.text, "Here is a plain-language answer.")
            finally:
                assistant.close()

    def test_offline_internet_search_falls_back_to_local_model(self):
        with tempfile.TemporaryDirectory() as directory:
            assistant = make_assistant(directory, llm_enabled=True)
            try:
                assistant.orchestrator.web_search.search = lambda *args, **kwargs: {
                    "query": "current weather",
                    "results": [],
                    "page_excerpts": [],
                    "count": 0,
                    "network_errors": {"ddg_html": "network unreachable"},
                    "connection_mode": "offline_fallback",
                    "live_sources_available": False,
                }
                assistant.llm.chat = lambda messages, system, **kwargs: "Offline answer; I cannot verify current conditions."
                result = assistant.process_text("Search about current weather")
                self.assertTrue(result.success, result.text)
                self.assertIn("OFFLINE FALLBACK", result.text)
                self.assertIn("cannot verify current conditions", result.text)
                self.assertEqual(result.data["connection_mode"], "offline_fallback")
                self.assertEqual(result.data["browser_retry_query"], "current weather")
            finally:
                assistant.close()

    def test_polyglot_coder_repairs_python_syntax_once_before_fallback(self):
        from types import SimpleNamespace

        class StubLLM:
            config = SimpleNamespace(keep_alive="5m")
            last_model_identity = "stub-coder"

            def __init__(self):
                self.calls = []

            def chat(self, messages, system, **kwargs):
                self.calls.append(messages[0]["content"])
                if len(self.calls) == 1:
                    return "```python\ndef broken(:\n    pass\n```"
                return "```python\ndef solve():\n    return 42\n```"

        with tempfile.TemporaryDirectory() as directory:
            llm = StubLLM()
            coder = PolyglotCoder(FileManager(SecurityConfig(allowed_roots=[Path(directory)])), llm)
            result = coder.generate_code(task="Return 42", language="python")
            self.assertEqual(len(llm.calls), 2)
            self.assertIn("failed the syntax check", llm.calls[1])
            self.assertTrue(result["syntax_valid"])
            self.assertIn("return 42", result["code"])

    def test_cached_shortcut_discovery_filters_uninstallers(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "Visual Studio Code.lnk").write_text("", encoding="ascii")
            (root / "Uninstall Visual Studio Code.lnk").write_text("", encoding="ascii")
            control = AppControl({}, extra_shortcut_dirs=[root])
            apps = control.discover_installed_apps(refresh=True, include_uwp=False)
            self.assertIn("Visual Studio Code", [app["name"] for app in apps])
            self.assertNotIn("Uninstall Visual Studio Code", [app["name"] for app in apps])

    def test_static_app_catalog_lists_configured_apps_without_platform_launch(self):
        control = AppControl({}, discover_installed=False)
        self.assertEqual(control.list_installed_apps()["count"], 0)


class HostilePageParsingTests(unittest.TestCase):
    """Parsers must stay bounded and must not invent results on bad markup."""

    def test_ddg_parser_caps_collected_results(self):
        page = "<a class='result__a' href='https://x.com/'>t</a>" * 5000
        parser = _DuckDuckGoHTMLParser()
        parser.feed(page)
        parser.close()
        self.assertEqual(len(parser.results), _DuckDuckGoHTMLParser.MAX_RESULTS)

    def test_ddg_parser_rejects_unsafe_schemes(self):
        for href in (
            "javascript:alert(1)",
            "data:text/html,<script>",
            "http://127.0.0.1/admin",
            "//duckduckgo.com/l/?uddg=javascript%3Aalert(1)",
        ):
            parser = _DuckDuckGoHTMLParser()
            parser.feed(f"<a class='result__a' href='{href}'>title</a>")
            parser.close()
            self.assertEqual(parser.results, [], href)

    def test_table_parser_is_bounded_but_keeps_normal_tables(self):
        rows = "".join(f"<tr><td>r{i}</td><td>{'x' * 200}</td></tr>" for i in range(5000))
        parser = TableParser()
        parser.feed(f"<table><tr><th>a</th><th>b</th></tr>{rows}</table>")
        parser.close()
        self.assertEqual(len(parser.tables), 1)
        self.assertEqual(len(parser.tables[0]["rows"]), parser.max_rows)
        self.assertEqual(parser.tables[0]["headers"], ["a", "b"])

    def test_price_parser_reads_symbol_and_labelled_bare_amounts(self):
        parser = PriceParser()
        parser.feed("<span>$49.99</span><span>Price: 1,299.00 USD</span>")
        parser.close()
        self.assertIn("$49.99", parser.prices)
        self.assertIn("1,299.00", parser.prices)

    def test_price_parser_ignores_unlabelled_numbers(self):
        parser = PriceParser()
        parser.feed("<span>4.5 stars from 120 reviews</span><span>Version 2.1.30</span>")
        parser.close()
        self.assertEqual(parser.prices, [])


if __name__ == "__main__":
    unittest.main()
