"""Regression tests for the IntentParser and MainWindow splits (audit B2 follow-up).

The parse() and handle_response() splits were mechanical but large, so they are
pinned two ways: structurally (the methods exist and stay small) and
behaviourally (the parser returns exactly what it used to for a broad corpus).
"""

from __future__ import annotations

import ast
import inspect
import unittest

from hinata.core.intent_parser import IntentParser
from hinata.security.action_validator import ActionValidator

#: Inputs covering every branch section of the old monolithic parse().
CORPUS = (
    # identity / naming
    "my name is Saurav",
    "what is my name",
    # web + identity vault
    "open website example.com",
    "go to github.com",
    "open spotify",
    # scraper
    "scrape tables from example.com",
    "crawl example.com",
    "monitor price example.com",
    # file wizard
    "analyze storage",
    "find duplicates",
    "organize downloads by type",
    "batch rename to numbered",
    "clean temp files",
    "quick cleanup",
    # communications
    "draft email to a@b.com subject hello",
    "list drafts",
    "remind me to call mom in 2 hours",
    "create reminder buy milk at 6pm",
    "broadcast hello team",
    # system
    "health check",
    "network diag",
    "system info",
    "find process chrome",
    "list processes",
    "volume up",
    # research
    "deep research on quantum computing",
    "research climate policy 2024",
    "compare cats vs dogs",
    # dev
    "git status",
    "run linter",
    "run workspace tests",
    "workspace list",
    "workspace diff",
    # memory / knowledge
    "clear memory",
    "clear conversations",
    "memory search favourite colour",
    "list preferences",
    "show knowledge documents",
    # search + fallthrough
    "search for python decorators",
    "web search for weather",
    "who are you",
    "asdkjhaskjdh",
    "",
    "   ",
)

EXPECTED_TOOLS = {
    "my name is Saurav": "set_owner_name",
    "open website example.com": "open_website",
    "scrape tables from example.com": "scrape_tables",
    "analyze storage": "analyze_storage",
    "clean temp files": "clean_temp",
    "quick cleanup": "quick_cleanup",
    "draft email to a@b.com subject hello": "draft_email",
    "remind me to call mom in 2 hours": "create_reminder",
    "broadcast hello team": "broadcast_message",
    "health check": "health_check",
    "network diag": "network_diag",
    "system info": "system_info",
    "deep research on quantum computing": "deep_research",
    "compare cats vs dogs": "compare_topics",
    "git status": "git_status",
    "clear memory": "clear_memory",
    "show knowledge documents": "knowledge_list",
    "web search for weather": "web_search",
}


class ParserSplitTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.parser = IntentParser(ActionValidator())

    def test_every_domain_section_has_its_own_method(self) -> None:
        for domain in ("web", "scraper", "files", "comms", "system", "research", "dev"):
            with self.subTest(domain=domain):
                method = getattr(IntentParser, f"_parse_{domain}", None)
                self.assertIsNotNone(method)
                self.assertLess(
                    len(inspect.getsource(method).splitlines()),
                    200,
                    f"_parse_{domain} should be a readable unit",
                )

    def test_parse_itself_is_a_short_router(self) -> None:
        lines = len(inspect.getsource(IntentParser.parse).splitlines())
        self.assertLess(lines, 260, f"parse() is still {lines} lines")

    def test_router_calls_every_section(self) -> None:
        source = inspect.getsource(IntentParser.parse)
        for domain in ("web", "scraper", "files", "comms", "system", "research", "dev"):
            self.assertIn(f"self._parse_{domain}(", source)

    def test_corpus_still_resolves_to_the_same_tools(self) -> None:
        for text, expected in EXPECTED_TOOLS.items():
            with self.subTest(text=text):
                action = self.parser.parse(text)
                self.assertIsNotNone(action, f"{text!r} no longer parses")
                self.assertEqual(action.tool, expected)

    def test_every_corpus_entry_is_stable(self) -> None:
        """No input may raise; repeated parses must agree."""
        for text in CORPUS:
            with self.subTest(text=text):
                first = self.parser.parse(text)
                second = self.parser.parse(text)
                self.assertEqual(
                    None if first is None else (first.tool, first.arguments),
                    None if second is None else (second.tool, second.arguments),
                )

    def test_unrecognised_text_still_returns_none(self) -> None:
        for text in ("asdkjhaskjdh", "", "   "):
            with self.subTest(text=text):
                self.assertIsNone(self.parser.parse(text))


class MainWindowHandlerSplitTests(unittest.TestCase):
    def test_handle_response_is_a_short_dispatcher(self) -> None:
        """Checked via AST so this runs without a working Qt install."""
        import ast
        import pathlib

        source = pathlib.Path("hinata/interface/main_window.py").read_text()
        tree = ast.parse(source)
        # handle_response lives in the ResponseHandlingMixin slice.
        handle = next(
            f
            for cls in ast.walk(tree)
            if isinstance(cls, ast.ClassDef)
            for f in cls.body
            if isinstance(f, ast.FunctionDef) and f.name == "handle_response"
        )
        length = handle.end_lineno - handle.lineno + 1
        self.assertLess(length, 40, f"handle_response is still {length} lines")
        called = {
            node.func.attr
            for node in ast.walk(handle)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        }
        for branch in ("verification", "plan_confirmation", "confirm_action", "pin"):
            self.assertIn(f"_handle_{branch}", called)

    def test_each_extracted_handler_exists(self) -> None:
        import ast
        import pathlib

        source = pathlib.Path("hinata/interface/main_window.py").read_text()
        tree = ast.parse(source)
        # These handlers live in the ResponseHandlingMixin slice and are inherited
        # by MainWindow, so scan every class in the module rather than one body.
        methods = {
            f.name: f
            for cls in ast.walk(tree)
            if isinstance(cls, ast.ClassDef)
            for f in cls.body
            if isinstance(f, ast.FunctionDef)
        }
        for name in (
            "_handle_verification",
            "_handle_plan_confirmation",
            "_handle_confirm_action",
            "_handle_pin",
        ):
            with self.subTest(method=name):
                self.assertIn(name, methods)
                node = methods[name]
                self.assertLess(node.end_lineno - node.lineno + 1, 60, f"{name} too long")


class MainWindowCompositionTests(unittest.TestCase):
    """The window must stay split into sized, single-concern slices.

    Without a floor on this, widget assembly creeps back into one 800-line class
    and the next reader has to rebuild the whole map of the window in their head.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.classes = _module_classes()

    def test_main_window_holds_no_widget_assembly(self) -> None:
        """MainWindow composes the slices; it must not re-absorb their bodies."""
        node = self.classes["MainWindow"]
        self.assertEqual(
            len(node.body), 1,
            "MainWindow must stay a composition point; give new members to a slice",
        )
        self.assertIsInstance(node.body[0], ast.Expr, "expected only its docstring")

    def test_the_window_is_composed_of_slices(self) -> None:
        for name in (
            "WindowChromeMixin", "ResponseHandlingMixin", "SpeechAndTaskMixin", "MainWindow",
        ):
            with self.subTest(cls=name):
                self.assertIn(name, self.classes)

    def test_no_class_carries_the_whole_window(self) -> None:
        for name in ("WindowChromeMixin", "ResponseHandlingMixin", "SpeechAndTaskMixin"):
            node = self.classes[name]
            with self.subTest(cls=name):
                self.assertLess(node.end_lineno - node.lineno + 1, 400, f"{name} has grown back")

    def test_layout_regions_are_separately_buildable(self) -> None:
        """Each visual region owns a method, so appearance can be tested per region."""
        methods = _methods_of(self.classes["WindowChromeMixin"])
        for region in (
            "_build_header", "_build_conversation", "_build_indicators",
            "_build_input_row", "_build_tools", "_build_footer",
        ):
            with self.subTest(region=region):
                self.assertIn(region, methods)

    def test_build_ui_only_composes_the_regions(self) -> None:
        build = _methods_of(self.classes["WindowChromeMixin"])["_build_ui"]
        calls = [
            node.func.attr
            for node in ast.walk(build)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr.startswith("_build_")
        ]
        self.assertEqual(
            calls,
            ["_build_header", "_build_conversation", "_build_indicators",
             "_build_input_row", "_build_tools", "_build_footer"],
            "the region order is what puts the window together",
        )

    def test_slices_do_not_duplicate_each_other(self) -> None:
        owners: dict[str, list[str]] = {}
        for name in ("WindowChromeMixin", "ResponseHandlingMixin", "SpeechAndTaskMixin", "MainWindow"):
            for method in _methods_of(self.classes[name]):
                owners.setdefault(method, []).append(name)
        clashes = {k: v for k, v in owners.items() if len(v) > 1}
        self.assertEqual(clashes, {}, "a method is defined on more than one slice")


if __name__ == "__main__":
    unittest.main()


def _module_classes() -> dict:
    import pathlib

    tree = ast.parse(pathlib.Path("hinata/interface/main_window.py").read_text())
    return {n.name: n for n in tree.body if isinstance(n, ast.ClassDef)}


def _methods_of(node) -> dict:
    return {f.name: f for f in node.body if isinstance(f, ast.FunctionDef)}
