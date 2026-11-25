"""Behavioural tests for FileWizard file operations (audit finding B1).

`file_wizard` had ~9% coverage while being the module that moves, renames,
converts and deletes user files. Every test here runs against a real temporary
directory through a real :class:`FileManager`, so the containment rules that
protect the rest of the app are exercised at the same time as the wizard logic.
"""

from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path

from hinata.automation.app_control import AutomationError
from hinata.automation.file_manager import FileManager
from hinata.automation.file_wizard import CATEGORY_MAP, FileWizard
from hinata.config import HinataConfig, SecurityConfig


class FileWizardTestBase(unittest.TestCase):
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
        self.files = FileManager(config.security)
        self.wizard = FileWizard(self.files)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _write(self, relative: str, content: str = "x", *, mtime: float | None = None) -> Path:
        path = self.documents / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        if mtime is not None:
            os.utime(path, (mtime, mtime))
        return path


class RequireFilesTests(FileWizardTestBase):
    def test_operations_need_a_file_manager(self) -> None:
        with self.assertRaises(AutomationError):
            FileWizard(None).analyze_storage("")


class AnalyzeStorageTests(FileWizardTestBase):
    def test_summarises_by_extension_and_size(self) -> None:
        self._write("a.txt", "a" * 100)
        self._write("b.txt", "b" * 200)
        self._write("c.png", "c" * 50)
        self._write("nested/d.py", "d" * 10)
        result = self.wizard.analyze_storage("")
        self.assertEqual(result["total_files"], 4)
        self.assertGreaterEqual(result["total_bytes"], 360)
        self.assertEqual(result["by_ext_count"][".txt"], 2)
        # largest-first ordering is what the UI shows
        self.assertEqual(Path(result["largest"][0]["path"]).name, "b.txt")

    def test_outside_root_is_refused(self) -> None:
        with self.assertRaises(AutomationError):
            self.wizard.analyze_storage(str(self.root))


class FindDuplicatesTests(FileWizardTestBase):
    def test_identical_files_are_grouped(self) -> None:
        body = "duplicate content " * 200
        self._write("one.txt", body)
        self._write("two.txt", body)
        self._write("unique.txt", "different " * 200)
        result = self.wizard.find_duplicates("", min_size_kb=0)
        groups = result["duplicate_groups"]
        self.assertTrue(groups)
        names = {Path(p).name for group in groups for p in group["files"]}
        self.assertEqual(names, {"one.txt", "two.txt"})

    def test_same_first_block_different_tail_is_not_a_duplicate(self) -> None:
        # Both files share their first 4096 bytes; only the full hash can separate them.
        head = "H" * 5000
        self._write("alpha.txt", head + "tail-one")
        self._write("beta.txt", head + "tail-two")
        result = self.wizard.find_duplicates("", min_size_kb=0)
        self.assertEqual(result["duplicate_groups"], [])

    def test_min_size_filter_is_respected(self) -> None:
        self._write("small1.txt", "tiny")
        self._write("small2.txt", "tiny")
        self.assertEqual(
            self.wizard.find_duplicates("", min_size_kb=1)["duplicate_groups"], []
        )


class OrganizeFolderTests(FileWizardTestBase):
    def test_dry_run_plans_without_moving(self) -> None:
        self._write("song.mp3")
        self._write("report.pdf")
        result = self.wizard.organize_folder("", dry_run=True)
        self.assertTrue(result["dry_run"])
        self.assertEqual(result["count"], 2)
        self.assertTrue((self.documents / "song.mp3").exists())
        targets = {Path(m["to"]).parent.name for m in result["planned_moves"]}
        self.assertEqual(targets, {"Audio", "Documents"})

    def test_real_run_moves_files_by_type(self) -> None:
        self._write("song.mp3")
        self._write("report.pdf")
        self.wizard.organize_folder("", dry_run=False)
        self.assertTrue((self.documents / "Audio" / "song.mp3").exists())
        self.assertTrue((self.documents / "Documents" / "report.pdf").exists())
        self.assertFalse((self.documents / "song.mp3").exists())

    def test_by_ext_and_by_date_modes(self) -> None:
        self._write("a.log")
        result = self.wizard.organize_folder("", mode="by_ext", dry_run=True)
        self.assertEqual(Path(result["planned_moves"][0]["to"]).parent.name, "log")
        stamp = time.mktime((2024, 3, 15, 12, 0, 0, 0, 0, -1))
        self._write("b.txt", mtime=stamp)
        result = self.wizard.organize_folder("", mode="by_date", dry_run=True)
        self.assertIn("2024-03", {Path(m["to"]).parent.name for m in result["planned_moves"]})

    def test_name_collision_does_not_overwrite(self) -> None:
        self._write("song.mp3", "first")
        (self.documents / "Audio").mkdir()
        (self.documents / "Audio" / "song.mp3").write_text("existing", encoding="utf-8")
        self.wizard.organize_folder("", dry_run=False)
        self.assertEqual(
            (self.documents / "Audio" / "song.mp3").read_text(encoding="utf-8"), "existing"
        )
        self.assertTrue((self.documents / "Audio" / "song_dup.mp3").exists())

    def test_hidden_files_are_untouched(self) -> None:
        self._write(".secret")
        self.assertEqual(self.wizard.organize_folder("", dry_run=True)["count"], 0)

    def test_empty_path_targets_the_first_permitted_root(self) -> None:
        self._write("a.log")
        result = self.wizard.organize_folder("", dry_run=True)
        self.assertEqual(result["count"], 1)
        self.assertEqual(Path(result["root"]), self.documents.resolve())

    def test_symlinks_are_left_in_place(self) -> None:
        import os

        target = self.root / "outside.mp3"
        target.write_text("audio", encoding="utf-8")
        os.symlink(target, self.documents / "linked.mp3")
        self._write("real.mp3")
        result = self.wizard.organize_folder("", dry_run=False)
        self.assertEqual(result["count"], 1)
        self.assertTrue((self.documents / "linked.mp3").is_symlink())
        self.assertTrue(target.exists())

    def test_invalid_mode_is_rejected(self) -> None:
        with self.assertRaises(AutomationError):
            self.wizard.organize_folder("", mode="by_planet")

    def test_file_path_instead_of_folder_is_rejected(self) -> None:
        target = self._write("notadir.txt")
        with self.assertRaises(AutomationError):
            self.wizard.organize_folder(str(target))

    def test_category_map_is_used(self) -> None:
        self.assertEqual(CATEGORY_MAP[".pdf"], "Documents")
        self.assertEqual(CATEGORY_MAP[".zip"], "Archives")
        self.assertEqual(self.wizard.organize_folder("", dry_run=True)["mode"], "by_type")


class BatchRenameTests(FileWizardTestBase):
    def test_numbered_dry_run(self) -> None:
        self._write("a.txt")
        self._write("b.txt")
        result = self.wizard.batch_rename("", "numbered", replacement="report", dry_run=True)
        planned = sorted(Path(r["to"]).name for r in result["planned"])
        self.assertEqual(planned, ["report_001.txt", "report_002.txt"])
        self.assertTrue((self.documents / "a.txt").exists())

    def test_numbered_applies(self) -> None:
        self._write("a.txt")
        self.wizard.batch_rename("", "numbered", replacement="doc", dry_run=False)
        self.assertTrue((self.documents / "doc_001.txt").exists())
        self.assertFalse((self.documents / "a.txt").exists())

    def test_find_replace_requires_pair(self) -> None:
        self._write("draft-final.txt")
        result = self.wizard.batch_rename(
            "", "find_replace", replacement="draft=release", dry_run=True
        )
        self.assertEqual(Path(result["planned"][0]["to"]).name, "release-final.txt")
        with self.assertRaises(AutomationError):
            self.wizard.batch_rename("", "find_replace", replacement="noseparator")

    def test_case_patterns_and_prefix(self) -> None:
        self._write("MixedCase.TXT")
        lower = self.wizard.batch_rename("", "lowercase", dry_run=True)
        self.assertEqual(Path(lower["planned"][0]["to"]).name, "mixedcase.txt")
        upper = self.wizard.batch_rename("", "uppercase", dry_run=True)
        self.assertEqual(Path(upper["planned"][0]["to"]).name, "MIXEDCASE.TXT")
        pref = self.wizard.batch_rename("", "prefix", replacement="2024_", dry_run=True)
        self.assertEqual(Path(pref["planned"][0]["to"]).name, "2024_MixedCase.TXT")

    def test_collision_aborts_instead_of_overwriting(self) -> None:
        self._write("Report.txt")          # will lower-case to report.txt
        self._write("report.txt.keep")     # placeholder to occupy the folder
        (self.documents / "report.txt").mkdir()
        with self.assertRaises(AutomationError):
            self.wizard.batch_rename("", "lowercase", dry_run=False)
        self.assertTrue((self.documents / "Report.txt").exists())

    def test_unknown_pattern_is_rejected(self) -> None:
        self._write("x.txt")
        with self.assertRaises(AutomationError):
            self.wizard.batch_rename("", "shuffle")

    def test_start_num_is_honoured(self) -> None:
        self._write("a.txt")
        result = self.wizard.batch_rename("", "numbered", replacement="f", start_num=7)
        self.assertEqual(Path(result["planned"][0]["to"]).name, "f_007.txt")


class ConvertFileTests(FileWizardTestBase):
    def test_markdown_to_html(self) -> None:
        self._write("notes.md", "# Title\n\nSome text.\n\n- one\n- two\n")
        result = self.wizard.convert_file("notes.md", "notes.html")
        self.assertEqual(result["converter"], "md->html")
        out = (self.documents / "notes.html").read_text(encoding="utf-8")
        self.assertIn("<h1", out)
        self.assertIn("Title", out)
        self.assertIn("<li>one</li>", out)

    def test_json_to_csv_and_back(self) -> None:
        (self.documents / "rows.json").write_text(
            '[{"name": "a", "size": 1}, {"name": "b", "size": 2}]', encoding="utf-8"
        )
        self.wizard.convert_file("rows.json", "rows.csv")
        csv_text = (self.documents / "rows.csv").read_text(encoding="utf-8")
        self.assertIn("name,size", csv_text)
        self.wizard.convert_file("rows.csv", "roundtrip.json")
        back = (self.documents / "roundtrip.json").read_text(encoding="utf-8")
        self.assertIn('"name": "a"', back)

    def test_json_scalar_is_rejected_for_csv(self) -> None:
        (self.documents / "scalar.json").write_text('"just a string"', encoding="utf-8")
        with self.assertRaises(AutomationError):
            self.wizard.convert_file("scalar.json", "scalar.csv")

    def test_text_cleanup_normalises_whitespace(self) -> None:
        (self.documents / "messy.txt").write_text("line one   \r\nline two  \r\n", encoding="utf-8")
        self.wizard.convert_file("messy.txt", "clean.txt")
        self.assertEqual(
            (self.documents / "clean.txt").read_text(encoding="utf-8"), "line one\nline two\n"
        )

    def test_destination_outside_roots_is_refused(self) -> None:
        self._write("a.md", "# x")
        with self.assertRaises(AutomationError):
            self.wizard.convert_file("a.md", str(self.root / "escape.html"))

    def test_existing_destination_is_not_overwritten(self) -> None:
        self._write("a.md", "# x")
        (self.documents / "b.html").write_text("keep me", encoding="utf-8")
        with self.assertRaises(AutomationError):
            self.wizard.convert_file("a.md", "b.html")
        self.assertEqual((self.documents / "b.html").read_text(encoding="utf-8"), "keep me")

    def test_source_symlink_cannot_be_converted(self) -> None:
        import os

        outside = self.root / "outside.md"
        outside.write_text("# secret", encoding="utf-8")
        os.symlink(outside, self.documents / "link.md")
        with self.assertRaises(AutomationError):
            self.wizard.convert_file("link.md", "link.html")

    def test_unsupported_conversion_is_rejected(self) -> None:
        self._write("a.md", "# x")
        with self.assertRaises(AutomationError):
            self.wizard.convert_file("a.md", "a.png")


class CleanTempTests(FileWizardTestBase):
    def test_old_files_are_reported_not_deleted_on_dry_run(self) -> None:
        old = time.time() - 30 * 86400
        self._write("old.tmp", mtime=old)
        self._write("recent.tmp")
        result = self.wizard.clean_temp("", min_age_days=7, dry_run=True)
        names = {Path(p["path"]).name for p in result["candidates"]}
        self.assertIn("old.tmp", names)
        self.assertNotIn("recent.tmp", names)
        self.assertTrue((self.documents / "old.tmp").exists())

    def test_fresh_log_and_in_progress_tmp_are_never_deleted(self) -> None:
        """Regression: extension matching used to bypass the age limit entirely."""
        self._write("app.log")            # 0 seconds old
        self._write("download.tmp")       # a download still in progress
        self.wizard.clean_temp("", min_age_days=7, dry_run=False)
        self.assertTrue((self.documents / "app.log").exists())
        self.assertTrue((self.documents / "download.tmp").exists())

    def test_apply_deletes_only_old_temp_artefacts(self) -> None:
        old = time.time() - 30 * 86400
        self._write("old.tmp", mtime=old)
        self._write("old.part", mtime=old)
        # A stale user document is not a temp file and must survive.
        self._write("keep.txt", mtime=old)
        self._write("report.pdf", mtime=old)
        self.wizard.clean_temp("", min_age_days=7, dry_run=False)
        self.assertFalse((self.documents / "old.tmp").exists())
        self.assertFalse((self.documents / "old.part").exists())
        self.assertTrue((self.documents / "keep.txt").exists())
        self.assertTrue((self.documents / "report.pdf").exists())

    def test_symlinks_are_skipped(self) -> None:
        import os

        outside = self.root / "outside.log"
        outside.write_text("victim", encoding="utf-8")
        os.utime(outside, (time.time() - 60 * 86400, time.time() - 60 * 86400))
        os.symlink(outside, self.documents / "linked.log")
        self.wizard.clean_temp("", min_age_days=7, dry_run=False)
        self.assertTrue(outside.exists(), "a symlink must never delete its target")


class AnalyzeStorageBoundTests(FileWizardTestBase):
    def test_non_positive_top_n_returns_no_rows(self) -> None:
        for index in range(5):
            self._write(f"f{index}.txt", "x" * ((index + 1) * 50))
        self.assertEqual(len(self.wizard.analyze_storage("", top_n=2)["largest"]), 2)
        # A negative bound used to slice from the end of the sorted list.
        self.assertEqual(self.wizard.analyze_storage("", top_n=-1)["largest"], [])

class ConvertFileFormatTests(FileWizardTestBase):
    def test_extension_drives_conversion_without_fmt(self) -> None:
        self._write("note.md", "# Title\n\nbody\n")
        result = self.wizard.convert_file(str(self.documents / "note.md"), str(self.documents / "out.html"))
        self.assertEqual(result["converter"], "md->html")
        self.assertTrue((self.documents / "out.html").is_file())

    def test_explicit_format_is_honoured_when_destination_has_no_extension(self) -> None:
        # ``fmt`` used to be accepted and silently ignored, so this wrote a
        # markdown->html failure instead of producing out.html.
        self._write("note2.md", "# Title\n")
        result = self.wizard.convert_file(
            str(self.documents / "note2.md"), str(self.documents / "plain"), fmt="html"
        )
        self.assertEqual(result["converter"], "md->html")
        self.assertTrue((self.documents / "plain.html").is_file())

    def test_explicit_format_conflicting_with_extension_is_refused(self) -> None:
        self._write("note3.md", "# Title\n")
        with self.assertRaises(AutomationError):
            self.wizard.convert_file(
                str(self.documents / "note3.md"), str(self.documents / "x.json"), fmt="html"
            )
        self.assertFalse((self.documents / "x.json").exists())


if __name__ == "__main__":
    unittest.main()
