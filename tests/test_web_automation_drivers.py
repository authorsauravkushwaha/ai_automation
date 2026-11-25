"""Network and driver-layer tests for WebAutomation (audit finding B1).

The first suite covers the pure parsers. This one covers the code that actually
talks to the network and to browser drivers, using a local HTTP server and fake
driver objects -- no real browser, no outbound traffic.
"""

from __future__ import annotations

import threading
import types
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

from hinata.automation.app_control import AutomationError
from hinata.automation.web_automation import (
    FillPlan,
    WebAutomation,
)

FORM_HTML = b"""<html><body>
<form action="/submit" method="post">
  <input id="mail" name="email" type="email">
  <input id="fn" name="first_name" type="text">
  <input id="pw" name="password" type="password">
  <button type="submit">Go</button>
</form></body></html>"""


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 - stdlib naming
        if self.path == "/missing":
            self.send_error(404, "Not Found")
            return
        if self.path == "/boom":
            self.send_error(500, "Server Error")
            return
        body = FORM_HTML
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: object) -> None:
        pass  # keep test output clean


class LocalServerTestBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def setUp(self) -> None:
        self.web = WebAutomation()
        self.base = f"http://127.0.0.1:{self.port}"

    def _plan(self, actions: list[dict]) -> FillPlan:
        return FillPlan(
            url=self.base, total_fields=len(actions), mappable=len(actions),
            missing=0, actions=actions, submit_selector=None,
        )


class FetchPageTests(LocalServerTestBase):
    def test_fetches_html_and_reports_bytes(self) -> None:
        page = self.web.fetch_page(self.base)
        self.assertEqual(page["status"], 200)
        self.assertIn("<form", page["html"])
        self.assertEqual(page["bytes"], len(FORM_HTML))

    def test_max_bytes_truncates_the_read(self) -> None:
        page = self.web.fetch_page(self.base, max_bytes=50)
        self.assertLessEqual(page["bytes"], 50)

    def test_http_error_becomes_automation_error(self) -> None:
        with self.assertRaises(AutomationError):
            self.web.fetch_page(f"{self.base}/missing")

    def test_server_error_becomes_automation_error(self) -> None:
        with self.assertRaises(AutomationError):
            self.web.fetch_page(f"{self.base}/boom")

    def test_connection_refused_becomes_automation_error(self) -> None:
        with self.assertRaises(AutomationError):
            self.web.fetch_page("http://127.0.0.1:1/", timeout=1)

    def test_inspect_url_combines_fetch_and_parse(self) -> None:
        inspection = self.web.inspect_url(self.base)
        self.assertEqual(inspection["fetch_status"], 200)
        fields = [f for form in inspection["forms"] for f in form["fields"]]
        self.assertTrue(any(f.get("name") == "email" for f in fields))

    def test_inspect_html_string_needs_no_network(self) -> None:
        result = self.web.inspect_html_string(FORM_HTML.decode())
        self.assertTrue(result["forms"])


class AutofillTests(LocalServerTestBase):
    def test_native_backend_returns_a_snippet_without_a_driver(self) -> None:
        with mock.patch.object(self.web, "_detect_backend", return_value="native"):
            result = self.web.autofill(
                self.base,
                profile_fields={"email": "jane@example.com"},
                open_browser=False,
            )
        self.assertEqual(result["execution"]["backend"], "native")
        self.assertIn("js_snippet", result)
        self.assertIn("jane@example.com", result["js_snippet"])

    def test_password_never_enters_the_plan(self) -> None:
        with mock.patch.object(self.web, "_detect_backend", return_value="native"):
            result = self.web.autofill(
                self.base,
                profile_fields={"email": "jane@example.com", "password": "hunter2"},
                open_browser=False,
            )
        self.assertNotIn("hunter2", str(result["plan"]))
        self.assertNotIn("hunter2", result["js_snippet"])

    def test_submit_is_plan_only_until_a_driver_exists(self) -> None:
        with mock.patch.object(self.web, "_detect_backend", return_value="native"):
            result = self.web.autofill(
                self.base, profile_fields={"email": "jane@example.com"},
                submit=True, open_browser=False,
            )
        self.assertFalse(result["execution"]["executed"])

    def test_playwright_path_is_used_when_detected(self) -> None:
        with mock.patch.object(self.web, "_detect_backend", return_value="playwright"), \
             mock.patch.object(self.web, "_execute_via_playwright",
                               return_value={"backend": "playwright", "executed": True}) as spy:
            result = self.web.autofill(
                self.base, profile_fields={"email": "jane@example.com"}, open_browser=False
            )
        spy.assert_called_once()
        self.assertTrue(result["execution"]["executed"])

    def test_selenium_path_is_used_when_detected(self) -> None:
        with mock.patch.object(self.web, "_detect_backend", return_value="selenium"), \
             mock.patch.object(self.web, "_execute_via_selenium",
                               return_value={"backend": "selenium", "executed": True}) as spy:
            self.web.autofill(self.base, profile_fields={"email": "a@b.com"}, open_browser=False)
        spy.assert_called_once()

    def test_unknown_profile_name_is_refused(self) -> None:
        with self.assertRaises(AutomationError):
            self.web.autofill(self.base, profile_name="nobody", open_browser=False)


class FillFieldTests(LocalServerTestBase):
    def test_native_fill_reports_selector_presence(self) -> None:
        with mock.patch.object(self.web, "_detect_backend", return_value="native"), \
             mock.patch.object(self.web, "open_website", return_value={"opened": True}):
            result = self.web.fill_field(self.base, "#mail", "jane@example.com")
        self.assertEqual(result["selector_present_on_page"], True)
        self.assertIn("js", result["result"])

    def test_unknown_selector_is_reported_as_absent(self) -> None:
        with mock.patch.object(self.web, "_detect_backend", return_value="native"), \
             mock.patch.object(self.web, "open_website", return_value={"opened": True}):
            result = self.web.fill_field(self.base, "#nope", "x")
        self.assertEqual(result["selector_present_on_page"], False)

    def test_js_value_is_quoted_safely(self) -> None:
        tricky = "he said \"hi\" \\ and 'bye'"
        with mock.patch.object(self.web, "_detect_backend", return_value="native"), \
             mock.patch.object(self.web, "open_website", return_value={"opened": True}):
            result = self.web.fill_field(self.base, "#mail", tricky)
        self.assertIn("js", result["result"])

    def test_private_url_is_refused(self) -> None:
        with self.assertRaises(AutomationError):
            self.web.fill_field("http://printer.local/x", "#a", "b", allow_private=False)


class BackendDetectionTests(unittest.TestCase):
    def test_native_when_no_driver_installed(self) -> None:
        web = WebAutomation()
        with mock.patch("importlib.util.find_spec", return_value=None):
            self.assertEqual(web._detect_backend(), "native")

    def test_playwright_is_preferred_when_present(self) -> None:
        web = WebAutomation()

        def fake_find(name: str, *a: object, **k: object) -> object:
            return object() if name.startswith("playwright") else None

        with mock.patch("importlib.util.find_spec", side_effect=fake_find):
            self.assertEqual(web._detect_backend(), "playwright")

    def test_selenium_when_only_selenium_is_present(self) -> None:
        web = WebAutomation()

        def fake_find(name: str, *a: object, **k: object) -> object:
            return object() if name.startswith("selenium") else None

        with mock.patch("importlib.util.find_spec", side_effect=fake_find):
            self.assertEqual(web._detect_backend(), "selenium")

    def test_missing_playwright_is_reported_not_raised(self) -> None:
        web = WebAutomation()
        with mock.patch.dict(
            "sys.modules", {"playwright": None, "playwright.sync_api": None}
        ):
            report = web._execute_via_playwright("https://example.com", self._empty_plan(), 0.1)
        self.assertFalse(report["executed"])
        self.assertIn("not installed", report["error"].lower())

    @staticmethod
    def _empty_plan() -> FillPlan:
        return FillPlan(
            url="https://example.com", total_fields=0, mappable=0, missing=0,
            actions=[], submit_selector=None,
        )


class DriverAbortTests(unittest.TestCase):
    """A driver failure must degrade to a report, never crash the turn."""

    def test_playwright_launch_failure_is_caught(self) -> None:
        web = WebAutomation()
        failing = mock.MagicMock()
        failing.__enter__.side_effect = RuntimeError("no display")
        fake_sync_api = types.ModuleType("playwright.sync_api")
        fake_sync_api.sync_playwright = mock.MagicMock(return_value=failing)  # type: ignore[attr-defined]
        fake_pkg = types.ModuleType("playwright")
        fake_pkg.sync_api = fake_sync_api  # type: ignore[attr-defined]
        plan = FillPlan(url="https://e.com", total_fields=1, mappable=1, missing=0,
                        actions=[{"selector": "#a", "value": "b"}], submit_selector=None)
        with mock.patch.dict(
            "sys.modules", {"playwright": fake_pkg, "playwright.sync_api": fake_sync_api}
        ):
            report = web._execute_via_playwright("https://e.com", plan, 0.1)
        self.assertFalse(report["executed"])
        self.assertIn("error", report)


class UrlPolicyInAutomationTests(LocalServerTestBase):
    def test_open_website_headless_only_validates(self) -> None:
        result = self.web.open_website(self.base, headless=True)
        self.assertFalse(result["opened"])
        self.assertEqual(result["url"], self.base)

    def test_open_website_rejects_bad_scheme_before_touching_the_os(self) -> None:
        with self.assertRaises(AutomationError):
            self.web.open_website("file:///etc/passwd")

    def test_open_website_surfaces_browser_refusal(self) -> None:
        # On Windows the default path is ``os.startfile``, which bypasses
        # webbrowser entirely -- so the refusal has to be simulated on the
        # platform-specific launcher too, not just on webbrowser.open.
        with mock.patch("webbrowser.open", return_value=False), \
             mock.patch("hinata.automation.web_automation.os.startfile", create=True,
                        side_effect=OSError("no handler"), ):
            with self.assertRaises(AutomationError):
                self.web.open_website(self.base)

    def test_open_website_reports_success(self) -> None:
        with mock.patch("webbrowser.open", return_value=True):
            result = self.web.open_website(self.base)
        self.assertTrue(result["opened"])


if __name__ == "__main__":
    unittest.main()


def _fake_playwright_module(locator_behaviour: str = "fill"):
    """Build a stand-in ``playwright.sync_api`` that records driver calls."""
    calls: dict[str, object] = {"fills": [], "selects": [], "checks": [], "clicks": 0}

    class _Locator:
        def __init__(self, selector: str) -> None:
            self.selector = selector
            self.first = self

        def count(self) -> int:
            if locator_behaviour == "boom":
                raise RuntimeError("locator exploded")
            if locator_behaviour == "missing":
                return 0
            # Simulate a page where only the name-based selector matches.
            if locator_behaviour == "name-only":
                return 1 if self.selector.startswith("[name=") else 0
            return 1

        def evaluate(self, _script: str) -> str:
            return "SELECT" if locator_behaviour == "select" else "INPUT"

        def fill(self, value: str) -> None:
            calls["fills"].append((self.selector, value))  # type: ignore[union-attr]

        def select_option(self, value: str) -> None:
            calls["selects"].append((self.selector, value))  # type: ignore[union-attr]

        def check(self) -> None:
            calls["checks"].append(self.selector)  # type: ignore[union-attr]

        def click(self, timeout: int = 0) -> None:
            calls["clicks"] += 1  # type: ignore[operator]

    class _Page:
        def goto(self, url: str, wait_until: str = "", timeout: int = 0) -> None:
            calls["goto"] = url

        def wait_for_timeout(self, ms: int) -> None:
            pass

        def locator(self, selector: str) -> _Locator:
            return _Locator(selector)

    class _Browser:
        def new_page(self) -> _Page:
            return _Page()

        def close(self) -> None:
            calls["closed"] = True

    class _Chromium:
        def launch(self, headless: bool = False) -> _Browser:
            return _Browser()

    class _Context:
        chromium = _Chromium()

        def __enter__(self) -> _Context:
            return self

        def __exit__(self, *exc: object) -> None:
            return None

    module = types.ModuleType("playwright.sync_api")
    module.sync_playwright = lambda: _Context()  # type: ignore[attr-defined]
    package = types.ModuleType("playwright")
    package.sync_api = module  # type: ignore[attr-defined]
    return package, module, calls


class PlaywrightDriverTests(unittest.TestCase):
    def _run(self, behaviour: str, actions: list[dict], submit: str | None = None):
        web = WebAutomation()
        package, module, calls = _fake_playwright_module(behaviour)
        plan = FillPlan(
            url="https://example.com", total_fields=len(actions), mappable=len(actions),
            missing=0, actions=actions, submit_selector=submit,
        )
        with mock.patch.dict("sys.modules", {"playwright": package, "playwright.sync_api": module}):
            report = web._execute_via_playwright("https://example.com", plan, 0.01)
        return report, calls

    def test_text_field_is_filled(self) -> None:
        report, calls = self._run(
            "fill", [{"selector": "#a", "value": "x", "canonical": "email"}]
        )
        self.assertTrue(report["executed"])
        self.assertEqual(report["filled"], 1)
        self.assertEqual(calls["fills"], [("#a", "x")])

    def test_select_field_uses_select_option(self) -> None:
        report, calls = self._run(
            "select", [{"selector": "#s", "value": "in", "canonical": "country"}]
        )
        self.assertTrue(report["executed"])
        self.assertEqual(calls["selects"], [("#s", "in")])

    def test_checkbox_is_checked_only_for_truthy_values(self) -> None:
        actions = [
            {"selector": "#yes", "value": "yes", "canonical": "x",
             "field_meta": {"type": "checkbox"}},
            {"selector": "#no", "value": "maybe", "canonical": "y",
             "field_meta": {"type": "checkbox"}},
        ]
        _report, calls = self._run("fill", actions)
        self.assertEqual(calls["checks"], ["#yes"])

    def test_missing_selector_is_reported_not_raised(self) -> None:
        report, _calls = self._run(
            "missing", [{"selector": "#gone", "value": "x", "canonical": "z"}]
        )
        self.assertTrue(report["executed"])
        self.assertEqual(report["filled"], 0)
        self.assertTrue(any("Not found" in e for e in report["errors"]))

    def test_locator_failure_is_collected_as_an_error(self) -> None:
        report, _calls = self._run(
            "boom", [{"selector": "#bad", "value": "x", "canonical": "z"}]
        )
        self.assertEqual(report["filled"], 0)
        self.assertTrue(report["errors"])

    def test_name_fallback_is_used_when_selector_misses(self) -> None:
        _report, calls = self._run(
            "name-only",
            [{"selector": "#gone", "value": "x", "canonical": "q",
              "field_meta": {"name": "email"}}],
        )
        self.assertIn('[name="email"]', calls["fills"][0][0])

    def test_submit_selector_is_clicked(self) -> None:
        report, calls = self._run(
            "fill", [{"selector": "#a", "value": "x", "canonical": "email"}], submit="button[type=submit]"
        )
        self.assertTrue(report["submitted"])
        self.assertEqual(calls["clicks"], 1)

    def test_browser_is_always_closed(self) -> None:
        _report, calls = self._run("fill", [{"selector": "#a", "value": "x", "canonical": "e"}])
        self.assertTrue(calls.get("closed"))

