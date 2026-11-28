"""Second-pass boundary tests: real subprocesses, synthetic networking, local data."""
from __future__ import annotations

import base64
import hashlib
import hmac
import importlib.util
import io
import json
import os
import socket
import sqlite3
import ssl
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request
import zipfile
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

from hinata.automation.app_control import AutomationError
from hinata.automation.web_automation import FillPlan, WebAutomation
from hinata.automation.workspace import IsolatedSandboxRunner
from hinata.core.types import ActionRequest
from hinata.security.browser_egress import BrowserEgress, guarded_page
from hinata.security.local_protection import (
    LocalVault,
    ProtectionError,
    decrypt_envelope,
    encrypt_envelope,
)
from hinata.security.net_guards import (
    PublicHTTPSConnection,
    build_public_web_opener,
    public_connection,
)
from hinata.security.process_output import OutputLimitError, checked_output
from hinata.security.vault_migration import migrate_schedule_envelopes
from tests.helpers import make_assistant


def legacy_fixture(plaintext: bytes, secret: bytes, aad: bytes) -> str:
    """Independent old-format fixture encoder; production can only WRITE v2."""
    salt, nonce = bytes(range(16)), bytes(range(16, 32))
    derived = hashlib.pbkdf2_hmac("sha256", secret, salt, 210000, dklen=64)
    stream = b"".join(hmac.new(derived[:32], nonce + i.to_bytes(8, "big"), hashlib.sha256).digest()
                      for i in range((len(plaintext) + 31) // 32))
    encrypted = bytes(a ^ b for a, b in zip(plaintext, stream[:len(plaintext)], strict=True))
    tag = hmac.new(derived[32:], b"HINATA-ENC-V1|" + salt + nonce + len(aad).to_bytes(4, "big") + aad + encrypted,
                   hashlib.sha256).digest()
    return "enc:v1:" + base64.urlsafe_b64encode(salt + nonce + tag + encrypted).decode()


class ConnectTimePolicyTests(unittest.TestCase):
    def answer(self, ip="93.184.216.34"):
        return (socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 443))

    def test_only_one_resolution_and_numeric_connect(self):
        sock = Mock()
        with patch("socket.getaddrinfo", return_value=[self.answer()]) as resolve, patch("socket.socket", return_value=sock):
            self.assertIs(public_connection(("example.com", 443), timeout=1), sock)
        resolve.assert_called_once()
        sock.connect.assert_called_once_with(("93.184.216.34", 443))

    def test_rebinding_and_mixed_answers_fail_before_socket_creation(self):
        for addresses in ([self.answer("127.0.0.1")], [self.answer(), self.answer("169.254.169.254")]):
            with self.subTest(addresses=addresses), patch("socket.getaddrinfo", return_value=addresses), patch("socket.socket") as sock:
                with self.assertRaises(OSError):
                    public_connection(("public-looking.example", 443), 1)
                sock.assert_not_called()

    def test_ipv6_mapped_and_multicast_are_refused(self):
        for ip in ("::1", "::ffff:127.0.0.1", "ff0e::1", "fe80::1"):
            with self.subTest(ip=ip), patch("socket.getaddrinfo", return_value=[(socket.AF_INET6, socket.SOCK_STREAM, 6, "", (ip, 443, 0, 0))]):
                with self.assertRaises(OSError):
                    public_connection(("example.com", 443), 1)

    def test_https_keeps_hostname_verification_and_sni(self):
        connection = PublicHTTPSConnection("example.com")
        self.assertTrue(connection._context.check_hostname)
        self.assertEqual(connection._context.verify_mode, ssl.CERT_REQUIRED)
        connection._context = Mock()
        sock = Mock()
        connection._create_connection = Mock(return_value=sock)
        connection.connect()
        connection._context.wrap_socket.assert_called_once_with(sock, server_hostname="example.com")

    def test_public_opener_ignores_ambient_proxies(self):
        with patch.dict(os.environ, {"HTTPS_PROXY": "http://127.0.0.1:9999"}):
            opener = build_public_web_opener(lambda _: None)
        self.assertFalse(any(isinstance(handler, urllib.request.ProxyHandler) and handler.proxies for handler in opener.handlers))


class BrowserPolicyTests(unittest.TestCase):
    def route(self, url):
        route = Mock()
        route.request.url = url
        route.request.method = "GET"
        route.request.post_data_buffer = None
        route.request.all_headers.return_value = {"Accept-Encoding": "gzip", "Host": "wrong"}
        return route

    def response(self, body=b"safe"):
        response = io.BytesIO(body)
        response.code = 200
        response.headers = {"Content-Type": "text/html"}
        return response

    def test_same_origin_fulfilled_not_delegated_to_browser(self):
        policy = BrowserEgress("https://example.com/form")
        route = self.route("https://example.com/script.js")
        with patch.object(policy.opener, "open", return_value=self.response()) as opened:
            policy.handle(route)
        route.fulfill.assert_called_once()
        route.continue_.assert_not_called()
        route.fetch.assert_not_called()
        self.assertEqual(opened.call_args.args[0].get_header("Accept-encoding"), "identity")

    def test_redirect_subresource_and_form_cross_origin_blocked(self):
        policy = BrowserEgress("https://example.com/form")
        for url in ("http://169.254.169.254/", "https://attacker.example/submit", "file:///etc/passwd", "wss://example.com/socket"):
            route = self.route(url)
            with self.subTest(url=url), patch.object(policy.opener, "open") as opened:
                policy.handle(route)
                opened.assert_not_called()
                route.abort.assert_called_once()

    def test_cross_origin_redirect_is_rejected_before_fulfill(self):
        policy = BrowserEgress("https://example.com")
        route = self.route("https://example.com/redirect")
        response = self.response()
        response.code = 302
        response.headers["Location"] = "http://169.254.169.254/private"
        with patch.object(policy.opener, "open", return_value=response):
            policy.handle(route)
        route.fulfill.assert_not_called()
        route.abort.assert_called_once()

    def test_response_and_request_quotas_fail_closed(self):
        policy = BrowserEgress("https://example.com")
        route = self.route("https://example.com")
        with patch.object(policy.opener, "open", return_value=self.response(b"x" * 4_000_001)):
            policy.handle(route)
        route.abort.assert_called_once()
        route.fulfill.assert_not_called()
        policy.requests = 100
        route = self.route("https://example.com")
        with patch.object(policy.opener, "open") as opened:
            policy.handle(route)
        opened.assert_not_called()

    def test_websockets_closed(self):
        policy = BrowserEgress("https://example.com")
        websocket = Mock()
        policy.block_websocket(websocket)
        websocket.close.assert_called_once()
        self.assertEqual(policy.blocked, 1)

    def test_browser_context_installs_guards_before_page_and_always_closes(self):
        playwright = MagicMock()
        browser = playwright.chromium.launch.return_value
        with guarded_page(playwright, "https://example.com"):
            pass
        browser.new_context.assert_called_once_with(service_workers="block", accept_downloads=False)
        context = browser.new_context.return_value
        context.route.assert_called_once()
        context.route_web_socket.assert_called_once()
        browser.close.assert_called_once()
        self.assertEqual(playwright.chromium.launch.call_args.kwargs["proxy"]["server"], "http://127.0.0.1:9")

    def test_selenium_refuses_without_driver_import_or_network(self):
        web = WebAutomation()
        plan = FillPlan(url="https://example.com", total_fields=0, mappable=0, missing=0, actions=[], submit_selector=None)
        result = web._execute_via_selenium(plan.url, plan, 0)
        self.assertFalse(result["executed"])
        self.assertFalse(result["submitted"])


class ProcessSupervisionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        with patch("hinata.automation.workspace._linux_netns_supported", return_value=False):
            self.runner = IsolatedSandboxRunner()

    def test_noisy_child_hits_quota_before_unbounded_capture(self):
        started = time.monotonic()
        with self.assertRaises(AutomationError) as error:
            self.runner.run_command([sys.executable, "-c", "import os;\nwhile True: os.write(1, b'x'*4096)"],
                                    cwd=Path(self.temp.name), max_output_bytes=8192, timeout_seconds=10)
        self.assertIn("output quota", str(error.exception))
        self.assertLess(time.monotonic() - started, 5)

    def test_timeout_stops_sleeping_child(self):
        started = time.monotonic()
        with self.assertRaises(AutomationError):
            self.runner.run_command([sys.executable, "-c", "import time; time.sleep(20)"],
                                    cwd=Path(self.temp.name), timeout_seconds=1)
        self.assertLess(time.monotonic() - started, 5)

    @unittest.skipUnless(sys.platform.startswith("linux"), "Linux address-space limit")
    def test_memory_limit_applies_before_project_code(self):
        result = self.runner.run_command([sys.executable, "-c", "x=bytearray(350*1024*1024); print('UNBOUNDED')"],
                                         cwd=Path(self.temp.name), max_memory_mb=256)
        self.assertNotEqual(result["exit_code"], 0)
        self.assertNotIn("UNBOUNDED", result["stdout"])
        self.assertIn("MemoryError", result["stderr"])

    @unittest.skipIf(os.name == "nt", "POSIX inherited-pipe process group test")
    def test_dead_leader_with_live_pipe_child_does_not_hang(self):
        command = "import subprocess,sys; subprocess.Popen([sys.executable,'-c','import time; time.sleep(20)'])"
        started = time.monotonic()
        with self.assertRaises(subprocess.TimeoutExpired):
            checked_output([sys.executable, "-c", command], timeout=0.5)
        self.assertLess(time.monotonic() - started, 4)

    def test_developer_capture_bounds_both_streams(self):
        with self.assertRaises(OutputLimitError):
            checked_output([sys.executable, "-c", "import os; os.write(2,b'x'*100000)"], max_bytes=1024)


class LegacyCompatibilityCoreTests(unittest.TestCase):
    def test_legacy_decryption_remains_available_without_aead_library(self):
        token = legacy_fixture(b"old saved value", b"original key", b"old context")
        with patch("hinata.security.local_protection._aesgcm", side_effect=ImportError("not installed")):
            self.assertEqual(decrypt_envelope(token, b"original key", aad=b"old context"), b"old saved value")


@unittest.skipUnless(importlib.util.find_spec("cryptography"), "secure-storage extra not installed")
class AEADMigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.vault = LocalVault(self.root / "vault", enabled=True)
        self.key = self.vault.ensure_key()

    def test_v2_randomized_and_authenticated_with_context(self):
        one = encrypt_envelope(b"private data", self.key, aad=b"context")
        two = encrypt_envelope(b"private data", self.key, aad=b"context")
        self.assertTrue(one.startswith("enc:v2:"))
        self.assertNotEqual(one, two)
        self.assertEqual(decrypt_envelope(one, self.key, aad=b"context"), b"private data")
        for token, key, aad in ((one, b"wrong", b"context"), (one, self.key, b"other"),
                                 (one[:-4] + "AAAA", self.key, b"context")):
            with self.subTest(aad=aad), self.assertRaises(ProtectionError):
                decrypt_envelope(token, key, aad=aad)

    def test_legacy_read_and_upgrade_do_not_rotate_key(self):
        legacy = legacy_fixture(b"private data", self.key, b"context")
        self.assertEqual(self.vault.unprotect_text(legacy, context="context"), "private data")
        before = self.vault.key_path.read_bytes()
        upgraded = self.vault.protect_text(legacy, context="context")
        self.assertTrue(upgraded.startswith("enc:v2:"))
        self.assertEqual(self.vault.unprotect_text(upgraded, context="context"), "private data")
        self.assertEqual(self.vault.key_path.read_bytes(), before)

    def test_missing_key_never_creates_replacement(self):
        legacy = legacy_fixture(b"private data", self.key, b"")
        missing = LocalVault(self.root / "missing", enabled=True)
        with self.assertRaises(ProtectionError):
            missing.unprotect_text(legacy)
        self.assertFalse(missing.key_path.exists())

    def test_optional_library_absence_never_falls_back_to_v1(self):
        import builtins
        original = builtins.__import__
        def importing(name, *args, **kwargs):
            if name.startswith("cryptography"):
                raise ImportError("disabled for test")
            return original(name, *args, **kwargs)
        with patch("builtins.__import__", side_effect=importing), self.assertRaises(ProtectionError):
            encrypt_envelope(b"private", self.key)

    def database(self, corrupt=False):
        database = self.root / "schedule.db"
        with sqlite3.connect(database) as conn:
            conn.execute("CREATE TABLE scheduled_tasks(schedule_id TEXT PRIMARY KEY, request_text TEXT)")
            conn.execute("INSERT INTO scheduled_tasks VALUES(?,?)", ("one", legacy_fixture(b"system info", self.key, b"schedule:one")))
            conn.execute("INSERT INTO scheduled_tasks VALUES(?,?)", ("two", "enc:v1:broken" if corrupt else "system info"))
        return database

    def test_backup_first_transactional_migration_is_idempotent(self):
        database = self.database()
        backup = self.root / "before.db"
        result = migrate_schedule_envelopes(database, self.vault, backup, encrypt_plaintext=True)
        self.assertEqual(result["migrated_fields"], 2)
        with sqlite3.connect(backup) as conn:
            self.assertTrue(conn.execute("SELECT request_text FROM scheduled_tasks WHERE schedule_id='one'").fetchone()[0].startswith("enc:v1:"))
        with sqlite3.connect(database) as conn:
            for sid, text in conn.execute("SELECT * FROM scheduled_tasks"):
                self.assertTrue(text.startswith("enc:v2:"))
                self.assertEqual(self.vault.unprotect_text(text, context="schedule:" + sid), "system info")
        again = migrate_schedule_envelopes(database, self.vault, self.root / "second.db", encrypt_plaintext=True)
        self.assertEqual(again["migrated_fields"], 0)
        with self.assertRaises(ProtectionError):
            migrate_schedule_envelopes(database, self.vault, backup)

    def test_corrupt_legacy_row_rolls_back_every_update(self):
        database = self.database(corrupt=True)
        with sqlite3.connect(database) as conn:
            before = conn.execute("SELECT * FROM scheduled_tasks ORDER BY schedule_id").fetchall()
        with self.assertRaises(ProtectionError):
            migrate_schedule_envelopes(database, self.vault, self.root / "failed-backup.db")
        with sqlite3.connect(database) as conn:
            self.assertEqual(conn.execute("SELECT * FROM scheduled_tasks ORDER BY schedule_id").fetchall(), before)
        self.assertTrue((self.root / "failed-backup.db").is_file())

    def test_encrypted_backup_roundtrip(self):
        destination = self.root / "backup.json"
        self.vault.export_encrypted_json({"message": "private"}, "test passphrase only", destination)
        self.assertTrue(json.loads(destination.read_text())["envelope"].startswith("enc:v2:"))
        self.assertEqual(self.vault.import_encrypted_json(destination, "test passphrase only"), {"message": "private"})


class IndependentVerificationTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.assistant = make_assistant(directory.name)
        self.addCleanup(self.assistant.close)
        self.root = Path(directory.name) / "Documents"
        self.files = self.assistant.files
        self.reviewer = self.assistant.orchestrator.reviewer

    def test_move_verifies_preimage_digest_and_source_absence(self):
        (self.root / "source.txt").write_bytes(b"expected")
        action = ActionRequest("move_file", {"source": "Documents/source.txt", "destination": "Documents/target.txt"})
        evidence = self.files.move_or_rename(**action.arguments)
        self.assertEqual(self.reviewer.review((action,), evidence={1: evidence})[0].status, "verified")
        (self.root / "target.txt").write_bytes(b"changed")
        self.assertEqual(self.reviewer.review((action,), evidence={1: evidence})[0].status, "contradicted")
        self.assertEqual(self.reviewer.review((action,))[0].status, "unknown")

    def test_binary_copy_uses_bytes_not_text_extension(self):
        (self.root / "source.bin").write_bytes(b"\x00\xff\x01")
        action = ActionRequest("copy_file", {"source": "Documents/source.bin", "destination": "Documents/target.bin"})
        self.files.copy_file(**action.arguments)
        self.assertEqual(self.reviewer.review((action,))[0].status, "verified")

    def test_archive_manifest_detects_changed_and_extra_entries(self):
        (self.root / "source.txt").write_bytes(b"expected")
        action = ActionRequest("archive_files", {"source": "Documents/source.txt", "archive_path": "Documents/bundle.zip"})
        evidence = self.files.create_archive(**action.arguments)
        self.assertEqual(self.reviewer.review((action,), evidence={1: evidence})[0].status, "verified")
        with zipfile.ZipFile(self.root / "bundle.zip", "a") as archive:
            archive.writestr("extra.txt", "unexpected")
        self.assertEqual(self.reviewer.review((action,), evidence={1: evidence})[0].status, "contradicted")

    def test_failed_archive_has_no_partial_destination(self):
        source = self.root / "source"
        source.mkdir()
        (source / "large.txt").write_bytes(b"x" * (self.files.config.max_read_bytes + 1))
        with self.assertRaises(AutomationError):
            self.files.create_archive(str(source), "Documents/bundle.zip")
        self.assertFalse((self.root / "bundle.zip").exists())
        self.assertFalse(list(self.root.glob(".hinata-archive-*")))

    def test_patch_apply_and_revert_compare_live_files_not_just_db_state(self):
        (self.root / "code.py").write_text("old\n", encoding="utf-8")
        workspace = self.assistant.workspaces.register_workspace(self.root, name="test")["workspace_id"]
        proposed = self.assistant.workspaces.propose_patch(workspace=workspace, path="code.py", new_content="new\n", title="test patch")
        pid = proposed["patch_id"]
        self.assistant.workspaces.apply_patch(pid)
        action = ActionRequest("workspace_apply_patch", {"patch_id": pid})
        self.assertEqual(self.reviewer.review((action,))[0].status, "verified")
        (self.root / "code.py").write_text("modified later\n", encoding="utf-8")
        self.assertEqual(self.reviewer.review((action,))[0].status, "contradicted")
        self.assistant.workspaces.revert_patch(pid)
        revert = ActionRequest("workspace_revert_patch", {"patch_id": pid})
        self.assertEqual(self.reviewer.review((revert,))[0].status, "verified")
        (self.root / "code.py").write_text("wrong\n", encoding="utf-8")
        self.assertEqual(self.reviewer.review((revert,))[0].status, "contradicted")


if __name__ == "__main__":
    unittest.main()
