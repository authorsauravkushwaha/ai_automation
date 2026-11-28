"""Regression tests for the second 100-finding weakness audit.

Each test pins a defect reproduced against the tree at commit 9940e48. The
written evidence and disposition are in ``docs/WEAKNESS_AUDIT_ROUND_2.md``.
"""

from __future__ import annotations

import hashlib
import http.server
import math
import os
import shutil
import stat
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

from hinata.automation.app_control import AutomationError
from hinata.automation.browser_search import (
    _validate_browser_url,
    browser_key,
    open_browser_search,
)
from hinata.automation.communication_center import CommunicationCenter
from hinata.automation.file_manager import FileManager
from hinata.automation.file_wizard import FileWizard
from hinata.automation.jarvis_cockpit import DesktopCockpit
from hinata.automation.research_synthesizer import ResearchSynthesizer
from hinata.automation.smart_scraper import SmartScraper
from hinata.automation.system_tuner import SystemTuner
from hinata.automation.web_automation import WebAutomation, normalize_url
from hinata.automation.web_search import (
    MAX_DOWNLOAD_BYTES,
    WebSearchEngine,
    _clamp_float,
    _clamp_int,
    is_safe_public_url,
)
from hinata.automation.workspace import (
    IsolatedSandboxRunner,
)
from hinata.automation.workspace import (
    _clamp_float as workspace_clamp_float,
)
from hinata.automation.workspace import (
    _clamp_int as workspace_clamp_int,
)
from hinata.config import (
    AgenticConfig,
    Application,
    ConfigError,
    HinataConfig,
    InterfaceConfig,
    MemoryConfig,
    OllamaConfig,
    PersonalityConfig,
    SecurityConfig,
    SpeechConfig,
    WakeWordConfig,
    load_config,
)
from hinata.core.errors import CancellationToken, ClassifiedError
from hinata.core.llm_manager import _bounded_timeout as llm_bounded_timeout
from hinata.core.ollama_service import _bounded_timeout as ollama_bounded_timeout
from hinata.core.tool_registry import ToolRegistry
from hinata.core.types import ActionRequest, ActionResult, AssistantResponse
from hinata.database.database import Database
from hinata.security.authentication import Authentication
from hinata.security.local_protection import (
    LocalVault,
    _create_private_key_if_absent,
    read_private,
)
from hinata.security.offline_guard import (
    OfflineGuard,
    OfflinePolicyError,
    validate_public_domain,
)
from hinata.security.redaction import REDACTED, detect_secrets, redact_text

REPO_ROOT = Path(__file__).resolve().parent.parent

# Build synthetic provider-shaped fixtures at runtime. Keeping full token-like
# literals out of the source avoids GitHub push-protection false positives while
# still exercising the exact scanner regexes.
_FAKE_ALNUM = "1234567890abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"


def _fake_token(prefix: str, length: int) -> str:
    return prefix + (_FAKE_ALNUM * 4)[:length]


FAKE_GITLAB_TOKEN = _fake_token("gl" + "pat-", 20)
FAKE_NPM_TOKEN = _fake_token("npm" + "_", 36)
FAKE_HUGGINGFACE_TOKEN = _fake_token("hf" + "_", 36)
FAKE_SENDGRID_KEY = "S" + "G." + "a" * 17 + "." + "b" * 17
FAKE_TELEGRAM_TOKEN = "123456789" + ":AA" + (_FAKE_ALNUM * 2)[:35]
FAKE_SLACK_TOKEN = "slack " + _fake_token("xox" + "c-", 30)
FAKE_XAPP_TOKEN = _fake_token("xapp" + "-", 40)
FAKE_ACCOUNT_KEY = "abc" + "123=="
FAKE_CONNECTION_STRING = (
    "AzureWebJobs"
    + "Storage=DefaultEndpointsProtocol=https;AccountName=x;Account"
    + "Key="
    + FAKE_ACCOUNT_KEY
    + ";EndpointSuffix=core.windows.net"
)
FAKE_REDIS_URL = "redis" + "://:" + "s3" + "cr3t" + "@127.0.0.1:6379/0"
FAKE_AUTH_HEADER = "X-Auth-" + "Token: " + "abcdefghijklmnopqrstuvwxyz"


class NewCredentialRedactionTests(unittest.TestCase):
    """R2-001..R2-010: eight credential families leaked through redaction."""

    def test_new_token_families_are_all_detected_and_redacted(self) -> None:
        samples = {
            FAKE_GITLAB_TOKEN: "gitlab_token",
            FAKE_NPM_TOKEN: "npm_token",
            FAKE_HUGGINGFACE_TOKEN: "huggingface_token",
            FAKE_SENDGRID_KEY: "sendgrid_key",
            FAKE_TELEGRAM_TOKEN: "telegram_bot_token",
            FAKE_SLACK_TOKEN: "slack_token",
            FAKE_XAPP_TOKEN: "slack_app_token",
        }
        for text, kind in samples.items():
            with self.subTest(kind=kind):
                self.assertIn(kind, detect_secrets(text))
                self.assertNotIn(text.split()[-1] if " " in text else text, redact_text(text))

    def test_cloud_account_key_and_connection_string_are_redacted(self) -> None:
        self.assertNotIn(
            FAKE_ACCOUNT_KEY,
            redact_text("Account" + "Key=" + FAKE_ACCOUNT_KEY),
        )
        connection = FAKE_CONNECTION_STRING
        cleaned = redact_text(connection)
        self.assertNotIn("abc123==", cleaned)
        self.assertIn("EndpointSuffix=core.windows.net", cleaned)

    def test_empty_username_url_credentials_are_redacted(self) -> None:
        cleaned = redact_text(FAKE_REDIS_URL)
        self.assertNotIn("s3cr3t", cleaned)
        self.assertIn("[REDACTED_CREDENTIALS]", cleaned)

    def test_extended_auth_headers_are_redacted(self) -> None:
        cleaned = redact_text(FAKE_AUTH_HEADER)
        self.assertNotIn("abcdefghijklmnopqrstuvwxyz", cleaned)

    def test_plain_digest_is_still_not_a_false_positive(self) -> None:
        digest = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
        self.assertIn(digest, redact_text(f"file sha256={digest}"))


class CentralResponseRedactionTests(unittest.TestCase):
    """R2-011..R2-013: one central boundary protects every response shape."""

    def test_assistant_response_redacts_text_and_data(self) -> None:
        token = FAKE_GITLAB_TOKEN
        response = AssistantResponse(
            f"could not read {token}",
            data={"password": "hunter2", "note": "ok"},
        )
        self.assertNotIn(token, response.text)
        self.assertEqual(response.data["password"], REDACTED)
        self.assertEqual(response.data["note"], "ok")

    def test_action_result_failure_redacts_message(self) -> None:
        token = FAKE_GITLAB_TOKEN
        result = ActionResult.failure("read_text_file", f"missing {token}")
        self.assertNotIn(token, result.message)

    def test_pending_action_is_not_rewritten_before_confirmation(self) -> None:
        token = FAKE_GITLAB_TOKEN
        request = ActionRequest("write_text_file", {"content": token})
        response = AssistantResponse("confirm please", action=request)
        self.assertIs(response.action, request)
        self.assertEqual(response.action.arguments["content"], token)


class PublicUrlPolicyTests(unittest.TestCase):
    """R2-014..R2-025: one fail-closed SSRF policy for every HTTP client."""

    def test_public_url_candidate_checks_are_deterministic(self) -> None:
        self.assertTrue(is_safe_public_url("https://example.com/path", resolve=False))
        self.assertTrue(is_safe_public_url("https://192.com", resolve=False))
        for bad in (
            "http://169.254.169.254/latest/meta-data/",
            "http://0x7f000001/",
            "http://2130706433/",
            "http://user:pass@example.com/",
            "http://example.com:99999/",
            "http://printer.local/",
        ):
            with self.subTest(url=bad):
                self.assertFalse(is_safe_public_url(bad, resolve=False))

    def test_loopback_exception_is_literal_only(self) -> None:
        self.assertTrue(is_safe_public_url("http://127.0.0.1:8000/", allow_loopback=True))
        self.assertTrue(is_safe_public_url("http://localhost/", allow_loopback=True))
        self.assertFalse(is_safe_public_url("http://localhost/", allow_loopback=False))
        self.assertFalse(is_safe_public_url("http://::1/", allow_loopback=True))

    def test_dns_answer_in_private_range_is_rejected(self) -> None:
        fake_answer = [(None, None, None, None, ("10.0.0.5", 80))]
        with patch("hinata.automation.web_search.socket.getaddrinfo", return_value=fake_answer):
            self.assertFalse(is_safe_public_url("http://example.com/"))

    def test_normalize_url_uses_the_shared_static_policy(self) -> None:
        self.assertEqual(normalize_url("https://example.com/path"), "https://example.com/path")
        self.assertEqual(normalize_url("https://192.com"), "https://192.com")
        self.assertEqual(normalize_url("http://127.0.0.1:8000/"), "http://127.0.0.1:8000/")
        for bad in (
            "http://169.254.169.254/",
            "http://0x7f000001/",
            "http://user:pass@example.com/",
            "http://printer.local/",
        ):
            with self.subTest(url=bad), self.assertRaises(AutomationError):
                normalize_url(bad)
        self.assertEqual(
            normalize_url("http://printer.local", allow_private=True),
            "http://printer.local",
        )

    def test_fetch_page_revalidates_redirect_targets(self) -> None:
        class RedirectHandler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 - http.server API
                self.send_response(302)
                self.send_header("Location", "http://169.254.169.254/latest/meta-data/")
                self.end_headers()

            def log_message(self, *args: object) -> None:
                return

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), RedirectHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            url = f"http://127.0.0.1:{server.server_address[1]}/"
            with self.assertRaises(AutomationError) as caught:
                WebAutomation().fetch_page(url)
            # Explicit loopback fetches now refuse every redirect before following it.
            self.assertIn("302", str(caught.exception))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_scraper_refuses_private_target_before_network(self) -> None:
        with self.assertRaises(AutomationError):
            SmartScraper().fetch("http://169.254.169.254/")
        with self.assertRaises(AutomationError):
            WebAutomation().click_element("http://10.0.0.5/admin", "#go")

    def test_crawl_pages_clamps_a_negative_budget(self) -> None:
        result = SmartScraper().crawl_pages("https://example.com", max_pages=-5)
        self.assertEqual(result["visited"], 0)
        self.assertEqual(result["pages"], [])


class AuthenticationHardeningTests(unittest.TestCase):
    """R2-026..R2-031: PIN verification was crash-prone and brute-forceable."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.database = Database(self.root / "hinata.db")
        self.auth = Authentication(self.database)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_malformed_pin_input_returns_false_instead_of_raising(self) -> None:
        self.auth.set_pin("2468")
        for value in ("１２３４", "abc", None, 2468, "", "123"):
            with self.subTest(value=value):
                self.assertFalse(self.auth.verify_pin(value))

    def test_corrupt_iteration_count_is_refused_without_pbkdf2(self) -> None:
        stored = "pbkdf2_sha256$999999999$" + "00" * 16 + "$" + "00" * 32
        self.database.set_setting(self.auth.SETTING_KEY, stored)
        self.assertFalse(self.auth.verify_pin("2468"))

    def test_repeated_failures_lock_and_then_expire(self) -> None:
        self.auth.set_pin("2468")
        now = 1_000.0
        with patch("hinata.security.authentication.time.time", return_value=now):
            for _ in range(self.auth.MAX_FAILED_ATTEMPTS):
                self.assertFalse(self.auth.verify_pin("0000"))
            self.assertGreater(self.auth.lockout_remaining(), 0)
            self.assertFalse(self.auth.verify_pin("2468"))
        with patch("hinata.security.authentication.time.time", return_value=now + 61.0):
            self.assertEqual(self.auth.lockout_remaining(), 0.0)
            self.assertTrue(self.auth.verify_pin("2468"))

    def test_setting_a_new_pin_clears_the_lockout(self) -> None:
        self.auth.set_pin("2468")
        with patch("hinata.security.authentication.time.time", return_value=1_000.0):
            for _ in range(self.auth.MAX_FAILED_ATTEMPTS):
                self.assertFalse(self.auth.verify_pin("0000"))
            self.assertGreater(self.auth.lockout_remaining(), 0)
            self.auth.set_pin("1357")
            self.assertEqual(self.auth.lockout_remaining(), 0.0)
            self.assertTrue(self.auth.verify_pin("1357"))


class DomainAllowlistTests(unittest.TestCase):
    """R2-032..R2-039: consented-online allowlist could match too broadly."""

    def test_public_domain_validation_rejects_ambiguous_entries(self) -> None:
        self.assertEqual(validate_public_domain("  Ollama.COM. "), "ollama.com")
        for bad in ("com", ".example.com", "*.example.com", "https://example.com", "127.0.0.1", "example.com/path", "printer.local"):
            with self.subTest(value=bad), self.assertRaises(ValueError):
                validate_public_domain(bad)

    def test_offline_guard_fails_closed_on_bad_allowlist(self) -> None:
        with self.assertRaises(ValueError):
            OfflineGuard(online_mode_enabled=True, allowed_online_domains=["com"])
        guard = OfflineGuard(online_mode_enabled=True, allowed_online_domains=["example.com"])
        guard.check_url("https://api.example.com/data")
        with self.assertRaises(OfflinePolicyError):
            guard.check_url("https://example.com.evil.test/data")

    def test_check_url_rejects_non_http_credentials_and_bad_ports(self) -> None:
        guard = OfflineGuard()
        for bad in (
            "file:///etc/passwd",
            "http://user:pass@example.com/",
            "http://example.com:99999/",
        ):
            with self.subTest(url=bad), self.assertRaises(OfflinePolicyError):
                guard.check_url(bad)

    def test_config_rejects_unvalidated_sections(self) -> None:
        examples = (
            '[security]\nallowed_online_domains = ["com"]\n',
            "[wake_word]\nthreshold = 9\n",
            '[wake_word]\nthreshold = "0.5"\n',
            '[interface]\ndark_theme = "yes"\n',
            "[security]\nsearch_max_directories = -1\n",
        )
        for content in examples:
            with self.subTest(content=content):
                handle = tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False, encoding="utf-8")
                try:
                    with handle:
                        handle.write(content)
                    with self.assertRaises(ConfigError):
                        load_config(handle.name)
                finally:
                    Path(handle.name).unlink(missing_ok=True)

    def test_example_config_still_loads(self) -> None:
        config = load_config(REPO_ROOT / "config.example.toml")
        self.assertEqual(config.wake_word.threshold, 0.5)
        self.assertEqual(config.security.allowed_online_domains, [])


class CommunicationTruthfulnessTests(unittest.TestCase):
    """R2-040..R2-047: silent failures and unbounded reminder inputs."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.database = Database(self.root / "hinata.db")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_reminder_rejects_negative_repeat_nan_time_and_control_channel(self) -> None:
        center = CommunicationCenter(self.database)
        with self.assertRaises(AutomationError):
            center.create_reminder("t", "m", repeat_seconds=-1)
        with self.assertRaises(AutomationError):
            center.create_reminder("t", "m", when_epoch=float("nan"))
        with self.assertRaises(AutomationError):
            center.create_reminder("t", "m", channel="local\n")
        with self.assertRaises(AutomationError):
            center.create_reminder(123, "m")  # type: ignore[arg-type]

    def test_broadcast_reports_a_failed_local_write(self) -> None:
        token = FAKE_GITLAB_TOKEN

        class FailingFiles:
            def resolve(self, *_args: object, **_kwargs: object) -> Path:
                raise AutomationError(f"denied {token}")

        result = CommunicationCenter(files=FailingFiles()).broadcast("hello")
        self.assertFalse(result["local_file_written"])
        self.assertNotIn(token, result["local_file_error"])

    def test_draft_email_reports_autofill_failure(self) -> None:
        center = CommunicationCenter(self.database)
        variables = {
            "subject": "s",
            "name": "n",
            "topic": "t",
            "body": "b",
            "full_name": "f",
        }
        with patch(
            "hinata.core.identity_vault.IdentityVault",
            side_effect=RuntimeError("vault unavailable"),
        ):
            result = center.draft_email(
                "hr@example.com",
                template="follow_up",
                template_vars=variables,
            )
        self.assertIn("vault unavailable", result["autofill_warning"])

    def test_broadcast_filename_is_unique_within_one_second(self) -> None:
        root = self.root / "Documents"
        root.mkdir()
        config = HinataConfig(data_dir=self.root / "data", security=SecurityConfig(allowed_roots=[root]))
        config.validate()
        center = CommunicationCenter(files=FileManager(config.security))
        first = center.broadcast("one")
        second = center.broadcast("two")
        self.assertTrue(first["local_file_written"])
        self.assertTrue(second["local_file_written"])
        self.assertNotEqual(first["local_file"], second["local_file"])


class FileWizardHardeningTests(unittest.TestCase):
    """R2-048..R2-051: duplicate detection used MD5 and negative bounds."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.documents = self.root / "Documents"
        self.documents.mkdir()
        config = HinataConfig(
            data_dir=self.root / "data",
            security=SecurityConfig(allowed_roots=[self.documents]),
        )
        config.validate()
        self.wizard = FileWizard(FileManager(config.security))

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_duplicates_use_sha256(self) -> None:
        payload = b"duplicate content" * 100
        (self.documents / "a.bin").write_bytes(payload)
        (self.documents / "b.bin").write_bytes(payload)
        result = self.wizard.find_duplicates("", min_size_kb=0)
        self.assertEqual(result["hash_algorithm"], "sha256")
        self.assertEqual(len(result["duplicate_groups"]), 1)
        expected = hashlib.sha256(payload).hexdigest()[:12]
        self.assertEqual(result["duplicate_groups"][0]["hash"], expected)

    def test_non_positive_group_limit_and_size_floor_are_safe(self) -> None:
        payload = b"same"
        (self.documents / "a.bin").write_bytes(payload)
        (self.documents / "b.bin").write_bytes(payload)
        self.assertEqual(self.wizard.find_duplicates("", max_groups=0)["duplicate_groups"], [])
        result = self.wizard.find_duplicates("", min_size_kb=-100)
        self.assertEqual(len(result["duplicate_groups"]), 1)


class VaultKeyCreationTests(unittest.TestCase):
    """R2-052..R2-054: concurrent key creation could lose encrypted data."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_key_creation_is_exclusive_and_private(self) -> None:
        path = self.root / "vault.key"
        self.assertTrue(_create_private_key_if_absent(path, b"A" * 32))
        original = path.read_bytes()
        self.assertFalse(_create_private_key_if_absent(path, b"B" * 32))
        self.assertEqual(path.read_bytes(), original)
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_key_creation_preserves_every_protected_payload_byte(self) -> None:
        path = self.root / "binary.key"
        # Include LF, CR and Ctrl-Z regardless of the random DPAPI output.
        payload = bytes(range(256))
        with patch("hinata.security.local_protection.protect_bytes", return_value=payload):
            self.assertTrue(_create_private_key_if_absent(path, b"A" * 32))
        self.assertEqual(path.read_bytes(), payload)

    def test_two_vault_instances_converge_on_one_key(self) -> None:
        first = LocalVault(self.root, enabled=True)
        second = LocalVault(self.root, enabled=True)
        self.assertEqual(first.ensure_key(), second.ensure_key())
        self.assertEqual(read_private(self.root / "vault.key"), first.ensure_key())


class SandboxRunnerHardeningTests(unittest.TestCase):
    """R2-055..R2-058: preexec_fn is unsafe in a threaded app."""

    def test_no_preexec_fn_and_no_hardcoded_tmp(self) -> None:
        class FakeResource:
            RLIMIT_CPU = 0
            RLIMIT_AS = 1
            RLIMIT_NOFILE = 2

            def __init__(self) -> None:
                self.calls: list[tuple[int, int, tuple[int, int]]] = []

            def prlimit(self, pid: int, resource_id: int, limits: tuple[int, int]) -> None:
                self.calls.append((pid, resource_id, limits))

            def setrlimit(self, *_args: object) -> None:
                return

        class FakeProc:
            import io
            stdout = io.BytesIO(b"")
            stderr = io.BytesIO(b"")
            pid = 424242
            _handle = 424242
            returncode = 0

            def poll(self) -> int:
                return 0

            def wait(self, timeout: float | None = None) -> int:
                return 0

        fake_resource = FakeResource()
        fake_proc = FakeProc()
        custom_tmp = tempfile.mkdtemp(prefix="round2-tmp-")
        self.addCleanup(shutil.rmtree, custom_tmp, ignore_errors=True)
        cwd = Path(custom_tmp) / "work"
        cwd.mkdir()
        with (
            patch("hinata.automation.workspace._RESOURCE", fake_resource),
            patch("hinata.automation.workspace._WindowsJobSandbox"),
            patch("hinata.automation.workspace._linux_netns_supported", return_value=False),
            patch("hinata.automation.workspace.tempfile.gettempdir", return_value=custom_tmp),
            patch.dict(os.environ, {"TEMP": "", "TMP": ""}, clear=False),
            patch("hinata.automation.workspace.subprocess.Popen", return_value=fake_proc) as popen,
        ):
            result = IsolatedSandboxRunner().run_command(
                [sys.executable, "-c", "pass"], cwd=cwd
            )
        self.assertEqual(result["exit_code"], 0)
        kwargs = popen.call_args.kwargs
        self.assertIsNone(kwargs.get("preexec_fn"))
        self.assertEqual(kwargs.get("start_new_session"), os.name != "nt")
        self.assertEqual(kwargs["env"]["TEMP"], custom_tmp)
        self.assertEqual(kwargs["env"]["TMP"], custom_tmp)
        if os.name != "nt":
            self.assertEqual(len(fake_resource.calls), 3)


class ConfigShapeHardeningTests(unittest.TestCase):
    """R2-059..R2-070: config sections accepted hostile shapes and NaN."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _validate(self, **sections: object) -> None:
        HinataConfig(data_dir=self.root / "data", **sections).validate()  # type: ignore[arg-type]

    def test_ollama_flags_integers_temperature_and_keep_alive_are_validated(self) -> None:
        bad_configs = (
            OllamaConfig(enabled="yes"),  # type: ignore[arg-type]
            OllamaConfig(strict_model_pin=1),  # type: ignore[arg-type]
            OllamaConfig(timeout_seconds=True),  # type: ignore[arg-type]
            OllamaConfig(temperature=float("nan")),
            OllamaConfig(temperature=3.0),
            OllamaConfig(keep_alive=-2),
        )
        for config in bad_configs:
            with self.subTest(config=config), self.assertRaises(ConfigError):
                self._validate(ollama=config)

    def test_loopback_urls_require_a_valid_port_and_plain_path(self) -> None:
        for host in (
            "http://127.0.0.1:99999/",
            "http://127.0.0.1/secret",
            "http://user@127.0.0.1:11434",
        ):
            with self.subTest(host=host):
                handle = tempfile.NamedTemporaryFile(
                    "w", suffix=".toml", delete=False, encoding="utf-8"
                )
                try:
                    with handle:
                        handle.write(f'[ollama]\nhost = "{host}"\n')
                    with self.assertRaises(ConfigError):
                        load_config(handle.name)
                finally:
                    Path(handle.name).unlink(missing_ok=True)

    def test_agentic_numbers_reject_bools_strings_and_nan(self) -> None:
        bad_configs = (
            AgenticConfig(max_plan_steps=True),  # type: ignore[arg-type]
            AgenticConfig(max_tool_calls="16"),  # type: ignore[arg-type]
            AgenticConfig(max_duration_seconds=float("nan")),
            AgenticConfig(per_tool_timeout_seconds=float("inf")),
        )
        for config in bad_configs:
            with self.subTest(config=config), self.assertRaises(ConfigError):
                self._validate(agentic=config)

    def test_security_speech_memory_and_interface_types_are_validated(self) -> None:
        bad_configs: tuple[tuple[str, object], ...] = (
            ("security", SecurityConfig(max_read_bytes=True)),  # type: ignore[arg-type]
            ("security", SecurityConfig(search_max_directories=1.5)),  # type: ignore[arg-type]
            ("security", SecurityConfig(allowed_roots=[None])),  # type: ignore[list-item]
            ("speech", SpeechConfig(tts_rate=True)),  # type: ignore[arg-type]
            ("speech", SpeechConfig(microphone=True)),  # type: ignore[arg-type]
            ("memory", MemoryConfig(conversation_limit=True)),  # type: ignore[arg-type]
            ("wake_word", WakeWordConfig(threshold=float("inf"))),
            ("interface", InterfaceConfig(dark_theme=1)),  # type: ignore[arg-type]
            ("personality", PersonalityConfig(mode=[])),  # type: ignore[arg-type]
        )
        for field, config in bad_configs:
            with self.subTest(field=field, config=config), self.assertRaises(ConfigError):
                self._validate(**{field: config})

    def test_application_command_parts_must_be_nonempty_strings(self) -> None:
        bad_apps = (
            Application(command=["ok"], process_names=[123]),  # type: ignore[list-item]
            Application(command=[""], process_names=[]),
        )
        for app in bad_apps:
            with self.subTest(app=app), self.assertRaises(ConfigError):
                self._validate(applications={"bad": app})


class CommunicationHardeningRound2Tests(unittest.TestCase):
    """R2-071..R2-076: draft headers and repeating-reminder arithmetic."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.database = Database(self.root / "hinata.db")
        self.center = CommunicationCenter(self.database)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_draft_email_rejects_header_injection_and_hostile_shapes(self) -> None:
        for recipient in (
            "hr@example.com\nBcc: attacker@example.com",
            "hr@example.com\r\nX-Injected: 1",
            "hr@example.com\x00",
        ):
            with self.subTest(recipient=recipient), self.assertRaises(AutomationError):
                self.center.draft_email(recipient, "subject")
        with self.assertRaises(AutomationError):
            self.center.draft_email(123, "subject")  # type: ignore[arg-type]
        with self.assertRaises(AutomationError):
            self.center.draft_email(
                "hr@example.com", "subject", template_vars={"x": 1}  # type: ignore[dict-item]
            )

    def test_fire_due_reminders_does_not_spin_on_tiny_interval(self) -> None:
        past = time.time() - 30
        self.center.create_reminder(
            "tiny", "message", when_epoch=past, repeat_seconds=0.001
        )
        now = time.time() + 120
        fired = self.center.fire_due_reminders(now_epoch=now)
        self.assertEqual(len(fired), 1)
        row = self.database.fetch_one(
            "SELECT remind_at_epoch FROM reminders WHERE reminder_id = ?",
            (fired[0]["reminder_id"],),
        )
        self.assertIsNotNone(row)
        assert row is not None
        next_at = float(row["remind_at_epoch"])
        self.assertGreater(next_at, now)
        self.assertLessEqual(next_at, now + 60)
        self.assertGreaterEqual(next_at - past, 60)
        with self.assertRaises(AutomationError):
            self.center.fire_due_reminders(now_epoch=float("nan"))


class SearchBoundHardeningTests(unittest.TestCase):
    """R2-077..R2-082: unbounded downloads, NaN timeouts and negative slices."""

    def test_search_clamp_helpers_reject_nan_and_bad_types(self) -> None:
        self.assertEqual(_clamp_int(-1, low=0, high=12, default=8), 0)
        self.assertEqual(_clamp_int(float("nan"), low=0, high=12, default=8), 8)
        self.assertEqual(_clamp_float(float("nan"), low=1.5, high=20.0, default=6.0), 6.0)
        self.assertEqual(_clamp_float("oops", low=1.5, high=20.0, default=6.0), 6.0)
        self.assertEqual(WebSearchEngine(timeout_seconds=float("nan")).timeout_seconds, 6.0)

    def test_http_get_never_passes_a_negative_read_limit(self) -> None:
        class FakeResponse:
            def __init__(self) -> None:
                self.headers = {"Content-Type": "text/plain"}
                self.read_sizes: list[int] = []

            def read(self, size: int) -> bytes:
                self.read_sizes.append(size)
                return b"ok"

            def __enter__(self) -> FakeResponse:
                return self

            def __exit__(self, *_args: object) -> None:
                return None

        class FakeOpener:
            def __init__(self, response: FakeResponse) -> None:
                self.response = response
                self.timeout: float | None = None

            def open(self, _request: object, *, timeout: float) -> FakeResponse:
                self.timeout = timeout
                return self.response

        response = FakeResponse()
        opener = FakeOpener(response)
        engine = WebSearchEngine()
        engine._opener = opener  # type: ignore[assignment]
        engine._check_policy = lambda url, *, resolve=True: None  # type: ignore[method-assign]
        self.assertEqual(engine._http_get("https://example.com", max_bytes=-1)[0], b"ok")
        self.assertEqual(engine._http_get("https://example.com", max_bytes=10**9)[0], b"ok")
        self.assertEqual(response.read_sizes, [0, MAX_DOWNLOAD_BYTES])
        self.assertEqual(opener.timeout, 6.0)

    def test_negative_provider_limits_return_empty_without_network(self) -> None:
        engine = WebSearchEngine()
        with patch.object(engine, "_http_get", return_value=(b"", "text/html")):
            self.assertEqual(engine._search_duckduckgo_html("q", limit=-1), [])
            self.assertEqual(engine._search_duckduckgo_lite("q", limit=-1), [])
        with patch.object(engine, "_http_get", return_value=(b"{}", "application/json")):
            self.assertEqual(engine._search_openlibrary("q", limit=-1), [])
        with patch.object(
            engine, "_http_get", return_value=(b'{"query":{"search":[]}}', "application/json")
        ):
            self.assertEqual(engine._search_wikipedia("q", limit=-1), [])


class ResearchSynthesizerHardeningTests(unittest.TestCase):
    """R2-083..R2-086: malformed results and config-less LLMs crashed synthesis."""

    def test_malformed_results_and_configless_llm_are_safe(self) -> None:
        class FakeSearch:
            def search(self, _query: str, *, max_results: int = 8) -> dict[str, Any]:
                return {"results": [{"url": "https://example.com"}]}

            def fetch_webpage(self, _url: str, *, max_chars: int = 4000) -> dict[str, Any]:
                return {"title": "Title", "content": "One. Two."}

        class ConfiglessLLM:
            pass

        synthesizer = ResearchSynthesizer(FakeSearch(), ConfiglessLLM())  # type: ignore[arg-type]
        research = synthesizer.deep_research(
            "topic", max_results_per_query=float("nan"), max_sources=-1
        )
        self.assertEqual(research["results_count"], 1)
        comparison = synthesizer.compare_topics("a", "b")
        self.assertIn("fallback", comparison["comparison"])
        summary = synthesizer.summarize_url(
            "https://example.com", max_chars=float("nan")
        )
        self.assertIn("One.", summary["summary"])
        with self.assertRaises(AutomationError):
            synthesizer.deep_research(123)  # type: ignore[arg-type]


class BrowserUrlValidationTests(unittest.TestCase):
    """R2-087..R2-089: the desktop browser accepted file/private URLs."""

    def test_only_public_http_urls_are_handed_to_a_browser(self) -> None:
        _validate_browser_url("https://example.com/search?q=x")
        for url in (
            "file:///etc/passwd",
            "http://127.0.0.1/",
            "http://[::1]/",
            "http://localhost/",
            "http://user:pass@example.com/",
            "https://example.com:99999/",
        ):
            with self.subTest(url=url), self.assertRaises(AutomationError):
                _validate_browser_url(url)
        with self.assertRaises(AutomationError):
            browser_key(123)  # type: ignore[arg-type]

    def test_default_browser_receives_only_a_validated_url(self) -> None:
        with (
            patch("hinata.automation.browser_search.sys.platform", "linux"),
            patch(
                "hinata.automation.browser_search.webbrowser.open",
                return_value=True,
            ) as opener,
        ):
            self.assertEqual(
                open_browser_search("https://example.com/search?q=x"),
                "your default browser",
            )
            with self.assertRaises(AutomationError):
                open_browser_search("file:///etc/passwd")
            self.assertEqual(opener.call_count, 1)


class SystemTunerBackupTests(unittest.TestCase):
    """R2-090: a negative retention window pruned every existing backup."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.documents = self.root / "Documents"
        self.documents.mkdir()
        config = HinataConfig(
            data_dir=self.root / "data",
            security=SecurityConfig(allowed_roots=[self.documents]),
        )
        config.validate()
        self.tuner = SystemTuner(files=FileManager(config.security), system=object())  # type: ignore[arg-type]

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_negative_backup_age_is_refused_before_deletion(self) -> None:
        source = self.documents / "src"
        source.mkdir()
        (source / "a.txt").write_text("x", encoding="utf-8")
        destination = self.documents / "Backups"
        destination.mkdir()
        old_backup = destination / "old.zip"
        old_backup.write_text("old", encoding="utf-8")
        with self.assertRaises(AutomationError):
            self.tuner.backup_create(str(source), str(destination), max_age_days=-1)
        self.assertTrue(old_backup.is_file())


class ToolRegistryTimeoutTests(unittest.TestCase):
    """R2-091..R2-092: zero/negative timeout overrides bypassed the guard."""

    def test_invalid_timeout_overrides_are_refused_before_the_handler(self) -> None:
        registry = ToolRegistry()
        called: list[ActionRequest] = []

        def handler(action: ActionRequest) -> ActionResult:
            called.append(action)
            return ActionResult(tool=action.tool, success=True, message="ok")

        registry.register("create_folder", handler)
        action = ActionRequest("create_folder", {})
        for override in (0, -1, float("nan"), 1e9, True, "bad"):
            with self.subTest(override=override), self.assertRaises(ClassifiedError):
                registry.invoke(action, timeout_override=override)  # type: ignore[arg-type]
        self.assertEqual(called, [])
        result = registry.invoke(action)
        self.assertTrue(result.success)
        self.assertEqual(len(called), 1)


class ClipboardAndCancellationHardeningTests(unittest.TestCase):
    """R2-093..R2-096: unbounded clipboard scans and silent callback failures."""

    def test_clipboard_scans_only_the_returned_prefix(self) -> None:
        class FakeResult:
            returncode = 0
            stdout = "A" * 100_000

        seen: list[int] = []

        def record(text: str) -> bool:
            seen.append(len(text))
            return False

        with (
            patch(
                "hinata.automation.jarvis_cockpit.shutil.which",
                return_value="/usr/bin/xclip",
            ),
            patch(
                "hinata.automation.jarvis_cockpit.subprocess.run",
                return_value=FakeResult(),
            ),
            patch(
                "hinata.automation.jarvis_cockpit.contains_secrets",
                side_effect=record,
            ),
        ):
            cockpit = DesktopCockpit()
            result = cockpit.read_clipboard(max_chars=10)
            self.assertEqual(result["length"], 100_000)
            self.assertTrue(result["truncated"])
            self.assertLessEqual(len(result["content"]), 10)
            self.assertEqual(seen, [10])
            with self.assertRaises(AutomationError):
                cockpit.read_clipboard(max_chars=float("nan"))
            with self.assertRaises(AutomationError):
                cockpit.write_clipboard(123)  # type: ignore[arg-type]

    def test_cancellation_callback_failures_are_logged_and_redacted(self) -> None:
        def bad_callback() -> None:
            raise RuntimeError(FAKE_GITLAB_TOKEN)

        token = CancellationToken()
        token.on_cancel(bad_callback)
        with patch("hinata.core.errors.LOGGER.error") as logger:
            token.cancel()
        self.assertTrue(logger.called)
        self.assertNotIn("glpat-", str(logger.call_args))

        already_cancelled = CancellationToken()
        already_cancelled.cancel()
        with patch("hinata.core.errors.LOGGER.error") as logger:
            already_cancelled.on_cancel(bad_callback)
        self.assertTrue(logger.called)


class RuntimeBoundHelperTests(unittest.TestCase):
    """R2-097..R2-100: shared timeout/budget helpers reject hostile values."""

    def test_timeout_helpers_reject_nan_negative_and_invalid_types(self) -> None:
        self.assertEqual(
            llm_bounded_timeout(float("nan"), low=1.0, high=300.0, default=5.0),
            5.0,
        )
        self.assertEqual(
            ollama_bounded_timeout(-1, low=0.1, high=10.0, default=0.4),
            0.1,
        )
        self.assertEqual(
            workspace_clamp_float(float("nan"), low=1.0, high=180.0, default=30.0),
            30.0,
        )
        self.assertEqual(
            workspace_clamp_int("bad", low=256, high=4096, default=1024),
            1024,
        )
        self.assertTrue(math.isfinite(_clamp_float(5, low=1.5, high=20.0, default=6.0)))


if __name__ == "__main__":
    unittest.main()
