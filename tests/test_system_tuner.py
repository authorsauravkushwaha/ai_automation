"""Behavioural tests for SystemTuner (audit finding B1).

`system_tuner` deletes files and prunes backups yet had ~11% coverage. These
tests run against a real temporary directory so the destructive paths are
actually exercised -- the previous absence of tests is exactly how the
"cleanup deletes a live log" bug survived.
"""

from __future__ import annotations

import os
import tempfile
import time
import unittest
import zipfile
from pathlib import Path

from hinata.automation.app_control import AutomationError
from hinata.automation.file_manager import FileManager
from hinata.automation.system_info import SystemInfo
from hinata.automation.system_tuner import SystemTuner
from hinata.config import HinataConfig, SecurityConfig


class SystemTunerTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.documents = self.root / "Documents"
        self.backups = self.documents / "Backups"
        self.documents.mkdir(parents=True)
        config = HinataConfig(
            data_dir=self.root / "data",
            security=SecurityConfig(allowed_roots=[self.documents]),
        )
        config.validate()
        self.files = FileManager(config.security)
        self.tuner = SystemTuner(self.files, SystemInfo())

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _write(self, relative: str, content: str = "x", *, age_days: float = 0) -> Path:
        path = self.documents / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        if age_days:
            stamp = time.time() - age_days * 86400
            os.utime(path, (stamp, stamp))
        return path


class HealthAndDiagnosticsTests(SystemTunerTestBase):
    def test_health_check_reports_real_values(self) -> None:
        result = self.tuner.health_check()
        self.assertIn(result["status"], {"healthy", "needs_attention"})
        self.assertIn("issues", result)
        self.assertIn("snapshot", result)
        self.assertIsInstance(result["ok"], list)

    def test_network_diag_probes_loopback_first(self) -> None:
        result = self.tuner.network_diag()
        self.assertEqual(result["checks"][0]["host"], "127.0.0.1")
        self.assertIn("loopback", result)
        self.assertIn("online", result)

    def test_network_diag_respects_strict_offline(self) -> None:
        """External probes must go through the guard, not around it."""
        from hinata.security.offline_guard import OfflineGuard

        guard = OfflineGuard(strict_offline=True)
        guard.install_socket_guard()
        try:
            result = self.tuner.network_diag()
        finally:
            guard.uninstall_socket_guard()
        external = [c for c in result["checks"] if c["host"] != "127.0.0.1"]
        self.assertTrue(external)
        self.assertTrue(all(not c["ok"] for c in external))
        self.assertFalse(result["online"])

    def test_process_search_refuses_short_names(self) -> None:
        with self.assertRaises(AutomationError):
            self.tuner.process_killer("a")

    def test_process_search_is_dry_run_only(self) -> None:
        result = self.tuner.process_killer("python")
        self.assertTrue(result["dry_run"])
        self.assertLessEqual(result["count"], 20)
        with self.assertRaises(AutomationError):
            self.tuner.process_killer("python", dry_run=False)


class BackupTests(SystemTunerTestBase):
    def test_backup_creates_archive_with_files(self) -> None:
        self._write("a.txt", "alpha")
        self._write("nested/b.txt", "beta")
        result = self.tuner.backup_create("Documents", "Documents/Backups")
        archive = Path(result["archive"])
        self.assertTrue(archive.exists())
        self.assertEqual(result["files"], 2)
        with zipfile.ZipFile(archive) as bundle:
            names = set(bundle.namelist())
        self.assertIn("a.txt", names)
        self.assertIn("nested/b.txt", names)

    def test_backup_prunes_only_old_archives(self) -> None:
        self._write("a.txt")
        self.backups.mkdir(parents=True)
        old = self.backups / "backup_Documents_old.zip"
        old.write_bytes(b"PK")
        stamp = time.time() - 90 * 86400
        os.utime(old, (stamp, stamp))
        result = self.tuner.backup_create("Documents", "Documents/Backups", max_age_days=30)
        self.assertGreaterEqual(result["pruned_old"], 1)
        self.assertFalse(old.exists())

    def test_backup_never_includes_previous_backups(self) -> None:
        """Regression: a backup inside its own source archived itself, then all
        earlier backups, growing the archive without bound."""
        self._write("a.txt", "alpha")
        first = self.tuner.backup_create("Documents", "Documents/Backups")
        time.sleep(1.05)  # archive names are second-resolution; sleep avoids a name clash
        second = self.tuner.backup_create("Documents", "Documents/Backups")
        with zipfile.ZipFile(second["archive"]) as bundle:
            names = bundle.namelist()
        self.assertEqual(names, ["a.txt"])
        self.assertEqual(second["files"], 1)
        # and the first archive is still a valid, self-contained backup
        with zipfile.ZipFile(first["archive"]) as bundle:
            self.assertNotIn("Backups/", "\n".join(bundle.namelist()))

    def test_backup_outside_roots_is_refused(self) -> None:
        with self.assertRaises(AutomationError):
            self.tuner.backup_create(str(self.root), "Documents/Backups")

    def test_missing_source_is_refused(self) -> None:
        with self.assertRaises(AutomationError):
            self.tuner.backup_create("Documents/does-not-exist", "Documents/Backups")


class QuickCleanupTests(SystemTunerTestBase):
    def test_dry_run_lists_without_deleting(self) -> None:
        self._write("stale.tmp", age_days=10)
        result = self.tuner.quick_cleanup("", dry_run=True)
        self.assertTrue(result["dry_run"])
        self.assertGreaterEqual(result["temp_count"], 1)
        self.assertTrue((self.documents / "stale.tmp").exists())

    def test_cleanup_removes_old_artefacts(self) -> None:
        self._write("stale.tmp", age_days=10)
        (self.documents / "empty_dir").mkdir()
        self.tuner.quick_cleanup("", dry_run=False, min_age_days=1)
        self.assertFalse((self.documents / "stale.tmp").exists())

    def test_cleanup_keeps_files_still_in_use(self) -> None:
        """Regression: a log written seconds ago used to be deleted."""
        self._write("live.log")           # 0 days old
        self._write("inprogress.tmp")     # a download in flight
        self.tuner.quick_cleanup("", dry_run=False, min_age_days=1)
        self.assertTrue((self.documents / "live.log").exists())
        self.assertTrue((self.documents / "inprogress.tmp").exists())

    def test_empty_directory_cleanup_is_bounded(self) -> None:
        (self.documents / "a" / "b").mkdir(parents=True)
        result = self.tuner.quick_cleanup("", dry_run=False)
        self.assertLessEqual(result["empty_count"], 50)

    def test_symlink_is_not_followed(self) -> None:
        victim = self.root / "victim.log"
        victim.write_text("keep me", encoding="utf-8")
        stamp = time.time() - 30 * 86400
        os.utime(victim, (stamp, stamp))
        os.symlink(victim, self.documents / "linked.log")
        self.tuner.quick_cleanup("", dry_run=False, min_age_days=1)
        self.assertTrue(victim.exists())

    def test_path_outside_roots_is_refused(self) -> None:
        with self.assertRaises(AutomationError):
            self.tuner.quick_cleanup(str(self.root))

    def test_requires_file_manager(self) -> None:
        with self.assertRaises(AutomationError):
            SystemTuner(None, SystemInfo()).quick_cleanup("")


class SameSecondBackupTests(SystemTunerTestBase):
    """The stamp has second resolution; real callers can collide within it."""

    def test_repeated_backup_in_one_second_picks_a_free_name(self) -> None:
        self._write("a.txt", "alpha")
        archives = [self.tuner.backup_create("Documents", "Documents/Backups")["archive"] for _ in range(3)]
        self.assertEqual(len(set(archives)), 3, archives)

    def test_same_second_backup_into_source_does_not_nest_itself(self) -> None:
        # Destination == source: the live archive used to be added to itself,
        # so each run nested the previous ones and grew without bound.
        self._write("a.txt", "alpha")
        for _ in range(3):
            result = self.tuner.backup_create("Documents", "Documents")
            with zipfile.ZipFile(result["archive"]) as bundle:
                self.assertEqual(bundle.namelist(), ["a.txt"])
            self.assertLess(result["bytes"], 1000, "archive must not contain other archives")


if __name__ == "__main__":
    unittest.main()
