"""Regression tests for the 2026-11-25 weakness audit.

Each test below pins a *previously demonstrated* defect so it cannot silently
return. The audit evidence (probe commands and observed output) is recorded in
``docs/WEAKNESS_AUDIT.md`` next to the finding ID used here.
"""

from __future__ import annotations

import datetime as dt
import tempfile
import time
import unittest
import urllib.request
from pathlib import Path

from hinata.automation.app_control import AutomationError
from hinata.automation.communication_center import CommunicationCenter
from hinata.automation.dev_assistant import DevAssistant
from hinata.automation.file_manager import FileManager
from hinata.automation.smart_scraper import _narrow_html_to_hint
from hinata.config import HinataConfig, SecurityConfig
from hinata.core.identity_vault import IdentityVault
from hinata.core.observability import TraceRecorder
from hinata.database.database import Database
from hinata.security.net_guards import (
    NoRedirectHandler,
    ValidatedRedirectHandler,
    build_loopback_opener,
    build_public_web_opener,
)
from hinata.security.offline_guard import OfflineGuard, OfflinePolicyError
from hinata.security.redaction import REDACTED, redact_text, redact_value


def _sandbox(tmp: Path) -> tuple[FileManager, Path]:
    """A FileManager whose only permitted root is ``tmp/Documents``."""
    document_root = tmp / "Documents"
    document_root.mkdir(parents=True, exist_ok=True)
    config = HinataConfig(
        data_dir=tmp / "data",
        security=SecurityConfig(allowed_roots=[document_root]),
    )
    config.validate()
    return FileManager(config.security), document_root


class DevAssistantWorkspaceContainmentTests(unittest.TestCase):
    """W-01: DevAssistant used to escape every permitted root."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.tmp = Path(self.temporary.name)
        self.files, self.documents = _sandbox(self.tmp)
        self.outside = self.tmp / "outside"
        self.outside.mkdir()
        (self.outside / "requirements.txt").write_text("requests==2.0\n", encoding="utf-8")
        self.dev = DevAssistant(files=self.files)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_outside_directory_is_refused_not_silently_accepted(self) -> None:
        with self.assertRaises(AutomationError) as caught:
            self.dev._resolve_workspace(str(self.outside))
        self.assertIn("approved", str(caught.exception).lower())

    def test_dependency_audit_cannot_disclose_outside_directory(self) -> None:
        # Before the fix this returned manifests from an unpermitted directory.
        with self.assertRaises(AutomationError):
            self.dev.dependency_audit(str(self.outside))

    def test_code_review_cannot_scan_outside_directory(self) -> None:
        (self.outside / "leak.py").write_text("password = 'x'\n", encoding="utf-8")
        with self.assertRaises(AutomationError):
            self.dev.code_review(workspace=str(self.outside))

    def test_parent_traversal_is_refused(self) -> None:
        with self.assertRaises(AutomationError):
            self.dev._resolve_workspace(str(self.documents / ".." / ".." / "etc"))

    def test_permitted_subdirectory_still_resolves(self) -> None:
        project = self.documents / "project"
        project.mkdir()
        self.assertEqual(self.dev._resolve_workspace(str(project)), project.resolve())

    def test_default_root_without_argument_is_contained(self) -> None:
        resolved = self.dev._resolve_workspace("")
        self.assertTrue(str(resolved).startswith(str(self.documents.resolve())))

    def test_git_status_reports_branch_and_change_count(self) -> None:
        import subprocess

        repo = self.documents / "repo"
        repo.mkdir()
        env = {
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@example.com",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@example.com",
            "PATH": "/usr/bin:/bin:/usr/local/bin",
        }
        try:
            subprocess.run(["git", "init", "-q"], cwd=repo, check=True, env=env, timeout=20)
            subprocess.run(
                ["git", "commit", "-q", "--allow-empty", "-m", "init"],
                cwd=repo, check=True, env=env, timeout=20, text=True,
            )
        except (OSError, subprocess.CalledProcessError):
            self.skipTest("git is not available in this environment")
        (repo / "new.txt").write_text("x", encoding="utf-8")
        result = self.dev.git_status(str(repo))
        self.assertEqual(result["changed_files"], 1)
        self.assertIn(result["branch"], {"master", "main"})


class RedactionLeakTests(unittest.TestCase):
    """W-05: secrets that the audit showed leaking through redaction."""

    def test_url_embedded_credentials_are_redacted(self) -> None:
        out = redact_text("postgres://user:s3cr3t@db.example.com:5432/prod")
        self.assertNotIn("s3cr3t", out)
        self.assertIn("db.example.com", out)

    def test_aws_secret_access_key_assignment_is_redacted(self) -> None:
        secret = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
        self.assertNotIn(secret, redact_text(f"aws_secret_access_key = {secret}"))

    def test_context_anchored_hex_key_is_redacted(self) -> None:
        key = "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6a7b8c9d0e1f2a3b4c5d6a7b8c9d0e1f2"
        self.assertNotIn(key, redact_text(f"encryption_key = {key}"))

    def test_plain_digest_is_not_a_false_positive(self) -> None:
        digest = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
        self.assertIn(digest, redact_text(f"file sha256={digest}"))

    def test_sensitive_dict_keys_are_redacted_regardless_of_value_shape(self) -> None:
        cleaned = redact_value(
            {
                "user": "bob",
                "password": "hunter2",
                "client_secret": "short",
                "encryption_key": "abc123",
                "note": "ok",
            }
        )
        self.assertEqual(cleaned["user"], "bob")
        self.assertEqual(cleaned["note"], "ok")
        for key in ("password", "client_secret", "encryption_key"):
            self.assertEqual(cleaned[key], REDACTED)


class OfflineGuardDnsTests(unittest.TestCase):
    """W-06: name resolution escaped strict offline mode."""

    def setUp(self) -> None:
        self.guard = OfflineGuard(strict_offline=True)
        self.guard.install_socket_guard()

    def tearDown(self) -> None:
        self.guard.uninstall_socket_guard()

    def test_public_dns_resolution_is_blocked(self) -> None:
        import socket

        with self.assertRaises(OfflinePolicyError):
            socket.getaddrinfo("example.com", 443)

    def test_loopback_resolution_still_works(self) -> None:
        import socket

        self.assertTrue(socket.getaddrinfo("localhost", 11434))
        self.assertTrue(socket.getaddrinfo("127.0.0.1", 11434))

    def test_blocked_resolution_is_recorded(self) -> None:
        import socket

        with self.assertRaises(OfflinePolicyError):
            socket.getaddrinfo("example.com", 80)
        self.assertTrue(
            any(e["purpose"] == "getaddrinfo" for e in self.guard.status()["recent_blocked"])
        )

    def test_uninstall_restores_original_resolver(self) -> None:
        import socket

        self.guard.uninstall_socket_guard()
        self.assertTrue(socket.getaddrinfo("localhost", 80))


class AuditChainConcurrencyTests(unittest.TestCase):
    """W-07: a second writer produced a false tamper alarm on a healthy chain."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temporary.name) / "audit.db"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_two_writers_keep_the_chain_intact(self) -> None:
        first = TraceRecorder(Database(self.db_path))
        run_id, _ = first.start_run(user_goal="goal", source="cli")
        second = TraceRecorder(Database(self.db_path))
        second.record_event(run_id=run_id, event_type="tool_call", status="ok", payload={"n": 1})
        first.record_event(run_id=run_id, event_type="tool_call", status="ok", payload={"n": 2})
        result = TraceRecorder(Database(self.db_path)).verify_integrity()
        self.assertTrue(result["intact"], result)

    def test_real_tampering_is_still_detected(self) -> None:
        recorder = TraceRecorder(Database(self.db_path))
        run_id, _ = recorder.start_run(user_goal="goal", source="cli")
        for index in range(3):
            recorder.record_event(
                run_id=run_id, event_type="tool_call", status="ok", payload={"n": index}
            )
        Database(self.db_path).execute("DELETE FROM agent_trace_events WHERE id = 2")
        self.assertFalse(TraceRecorder(Database(self.db_path)).verify_integrity()["intact"])


class ReminderTimeParsingTests(unittest.TestCase):
    """W-08: documented reminder phrasings were rejected."""

    def setUp(self) -> None:
        self.center = CommunicationCenter()

    def test_documented_phrasings_parse(self) -> None:
        for phrase in (
            "in 2 hours",
            "in 30 minutes",
            "tomorrow 3pm",
            "tomorrow",
            "next monday 10am",
            "friday 17:00",
            "tonight",
            "2026-12-01 10:00",
        ):
            with self.subTest(phrase=phrase):
                self.assertGreater(self.center._parse_when(phrase), time.time() - 61)

    def test_weekday_advances_to_next_occurrence(self) -> None:
        parsed = dt.datetime.fromtimestamp(self.center._parse_when("monday 9am"))
        self.assertEqual(parsed.weekday(), 0)
        self.assertGreater(parsed, dt.datetime.now())

    def test_invalid_times_are_still_rejected(self) -> None:
        for phrase in ("tomorrow 25:00", "gibberish", "in 0 parsecs"):
            with self.subTest(phrase=phrase):
                with self.assertRaises(AutomationError):
                    self.center._parse_when(phrase)


class NetGuardTests(unittest.TestCase):
    """W-21: four duplicated redirect handlers, now one audited module."""

    def test_loopback_opener_disables_proxies_and_redirects(self) -> None:
        opener = build_loopback_opener()
        self.assertTrue(any(isinstance(h, NoRedirectHandler) for h in opener.handlers))

    def test_public_opener_revalidates_two_hop_redirects(self) -> None:
        seen: list[str] = []

        def check(url: str) -> None:
            seen.append(url)
            if "169.254.169.254" in url:
                raise PermissionError("metadata endpoint")

        handler = ValidatedRedirectHandler(check)
        request = urllib.request.Request("https://ok.example/start")
        handler.redirect_request(request, None, 302, "Found", {}, "https://cdn.example/next")
        self.assertEqual(seen, ["https://cdn.example/next"])
        with self.assertRaises(PermissionError):
            handler.redirect_request(
                request, None, 302, "Found", {}, "http://169.254.169.254/latest/meta-data/"
            )

    def test_public_opener_installs_the_validated_handler(self) -> None:
        opener = build_public_web_opener(lambda url: None)
        self.assertTrue(any(isinstance(h, ValidatedRedirectHandler) for h in opener.handlers))


class IdentityVaultSecretTests(unittest.TestCase):
    """W-12: credential-shaped values could be stored in innocent-looking fields."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.vault = IdentityVault(Database(Path(self.temporary.name) / "vault.db"))

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_token_hidden_in_free_text_field_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            self.vault.save_profile(
                "leaky", {"note": "my sk-proj-abc123def456ghi789jkl012mno345 key"}
            )

    def test_card_numbers_are_refused(self) -> None:
        with self.assertRaises(ValueError):
            self.vault.save_profile("leaky", {"card_number": "4111111111111111"})

    def test_normal_identity_still_saves(self) -> None:
        profile = self.vault.save_profile(
            "personal",
            {"first_name": "Saurav", "last_name": "Kushwaha", "email": "s@example.com"},
        )
        self.assertEqual(profile.fields["first_name"], "Saurav")
        self.assertEqual(profile.fields["full_name"], "Saurav Kushwaha")


class ScraperHintTests(unittest.TestCase):
    """W-20: ``selector_hint`` was accepted and silently ignored."""

    HTML = (
        "<html><body><div class='cards'><ul><li>alpha</li><li>beta</li></ul></div>"
        "<div class='other'><ul><li>gamma</li></ul></div></body></html>"
    )

    def test_class_hint_narrows_the_region(self) -> None:
        narrowed = _narrow_html_to_hint(self.HTML, "div.cards")
        self.assertIn("alpha", narrowed)
        self.assertNotIn("gamma", narrowed)

    def test_id_and_compound_hints(self) -> None:
        html = (
            "<html><body><section id='main'><p>inside</p></section>"
            "<section><p>outside</p></section></body></html>"
        )
        self.assertIn("inside", _narrow_html_to_hint(html, "#main"))
        self.assertNotIn("outside", _narrow_html_to_hint(html, "#main"))
        nested = "<ul class='items'><li>a<ul><li>b</li></ul></li></ul><p>tail</p>"
        narrowed = _narrow_html_to_hint(nested, "ul.items")
        self.assertIn("<li>b</li>", narrowed)
        self.assertNotIn("tail", narrowed)

    def test_unmatched_hint_keeps_the_full_document(self) -> None:
        self.assertEqual(_narrow_html_to_hint(self.HTML, "div.nonexistent"), self.HTML)

    def test_empty_hint_is_a_noop(self) -> None:
        self.assertEqual(_narrow_html_to_hint(self.HTML, ""), self.HTML)


class ReminderAndDraftLimitTests(unittest.TestCase):
    """SQLite reads a negative LIMIT as "no limit", so these must clamp."""

    def setUp(self) -> None:
        self.db = Database(Path(tempfile.mkdtemp()) / "limits.db")
        self.center = CommunicationCenter(database=self.db)
        for index in range(5):
            self.center.draft_email(f"u{index}@example.com", "s", f"body {index}")
            self.center.create_reminder(f"r{index}", "msg", when_str="2030-01-01 10:00")

    def test_list_drafts_clamps_non_positive_limit(self) -> None:
        self.assertEqual(self.center.list_drafts(limit=2)["count"], 2)
        self.assertEqual(self.center.list_drafts(limit=0)["count"], 0)
        # limit=-1 used to return all 5 drafts.
        self.assertEqual(self.center.list_drafts(limit=-1)["count"], 0)

    def test_list_reminders_clamps_non_positive_limit(self) -> None:
        self.assertEqual(self.center.list_reminders(limit=2)["count"], 2)
        self.assertEqual(self.center.list_reminders(limit=-1)["count"], 0)
        self.assertEqual(self.center.list_reminders(limit=-1, pending_only=False)["count"], 0)

    def test_due_reminders_clamps_non_positive_limit(self) -> None:
        self.assertEqual(self.center.due_reminders(limit=-1), [])


if __name__ == "__main__":
    unittest.main()
