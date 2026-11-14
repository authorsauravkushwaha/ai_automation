import importlib.util
import io
import subprocess
import tempfile
import unittest
import warnings
import zipfile
from pathlib import Path
from unittest.mock import patch

from hinata.automation.app_control import AutomationError
from hinata.knowledge.documents import MAX_DOCX_XML_BYTES, _extract_pdf
from tests.helpers import make_assistant


class DocumentFormatTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.assistant = make_assistant(self.tmp.name)
        self.root = Path(self.tmp.name) / "Documents"

    def tearDown(self):
        self.assistant.close()
        self.tmp.cleanup()

    def make_docx(self, name, xml):
        path = self.root / name
        with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("word/document.xml", xml)
        return path

    def test_docx_import_approval_and_search(self):
        self.make_docx("notes.docx", b'''<?xml version="1.0" encoding="UTF-8"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>
<w:p><w:r><w:t>Orchid deadline</w:t></w:r><w:r><w:tab/><w:t>Friday</w:t></w:r></w:p>
<w:p><w:r><w:t>Second paragraph.</w:t></w:r></w:p></w:body></w:document>''')
        pending = self.assistant.process_text("index document Documents/notes.docx")
        self.assertTrue(pending.needs_confirmation)
        self.assertFalse(self.assistant.knowledge.search("orchid"))
        result = self.assistant.resume(pending.action)
        self.assertTrue(result.success, result.text)
        result = self.assistant.process_text("search knowledge for orchid")
        self.assertIn("Orchid deadline\tFriday", result.text)
        self.assertIn("notes.docx chunk 1", result.text)

    def test_rejects_entity_declarations_and_oversized_xml(self):
        evil = self.make_docx("entity.docx", b'<!DOCTYPE foo [<!ENTITY secret "oops">]><foo>&secret;</foo>')
        with self.assertRaises(AutomationError):
            self.assistant.knowledge.index(str(evil))
        oversized = self.make_docx("large.docx", b"a" * (MAX_DOCX_XML_BYTES + 1))
        with self.assertRaises(AutomationError):
            self.assistant.knowledge.index(str(oversized))
        self.assertFalse(self.assistant.knowledge.search("secret"))

    def test_rejects_multiple_main_documents_and_invalid_archive(self):
        path = self.root / "duplicate.docx"
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr("word/document.xml", "<a/>")
                archive.writestr("word/document.xml", "<a/>")
        with self.assertRaises(AutomationError):
            self.assistant.knowledge.index(str(path))
        path.write_text("not a zip", encoding="utf-8")
        with self.assertRaises(AutomationError):
            self.assistant.knowledge.index(str(path))

    def test_pdf_timeout_fails_closed(self):
        with patch("hinata.knowledge.documents.subprocess.run", side_effect=subprocess.TimeoutExpired("pdf", 15)):
            with self.assertRaisesRegex(AutomationError, "timed out"):
                _extract_pdf(b"pdf")

    def test_missing_pdf_dependency_has_setup_guidance(self):
        with patch("hinata.knowledge.documents.subprocess.run", return_value=subprocess.CompletedProcess([], 2, b"", b"")):
            with self.assertRaisesRegex(AutomationError, "documents"):
                _extract_pdf(b"pdf")

    @unittest.skipUnless(importlib.util.find_spec("pypdf"), "optional PDF dependency not installed")
    def test_actual_pdf_text_extraction_and_encrypted_rejection(self):
        from pypdf import PdfWriter
        from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

        writer = PdfWriter()
        page = writer.add_blank_page(width=300, height=200)
        font = DictionaryObject({NameObject("/Type"): NameObject("/Font"),
                                 NameObject("/Subtype"): NameObject("/Type1"),
                                 NameObject("/BaseFont"): NameObject("/Helvetica")})
        page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)})})
        content = DecodedStreamObject()
        content.set_data(b"BT /F1 12 Tf 20 100 Td (Jasmine review on Monday) Tj ET")
        page[NameObject("/Contents")] = writer._add_object(content)
        buf = io.BytesIO()
        writer.write(buf)
        path = self.root / "brief.pdf"
        path.write_bytes(buf.getvalue())
        self.assistant.knowledge.index(str(path))
        rows = self.assistant.knowledge.search("jasmine")
        self.assertTrue(rows)
        self.assertIn("Monday", rows[0]["excerpt"])
        encrypted = PdfWriter()
        encrypted.add_blank_page(width=200, height=200)
        encrypted.encrypt("secret")
        with path.open("wb") as handle:
            encrypted.write(handle)
        with self.assertRaisesRegex(AutomationError, "Encrypted"):
            self.assistant.knowledge.index(str(path))
        # The failed replacement leaves the original index transaction intact,
        # but stale-source checks prevent that old content from being returned.
        self.assertEqual(self.assistant.knowledge.search("jasmine"), [])


class FolderSyncTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.assistant = make_assistant(self.tmp.name)
        self.root = Path(self.tmp.name) / "Documents"
        self.folder = self.root / "library"
        self.folder.mkdir()

    def tearDown(self):
        self.assistant.close()
        self.tmp.cleanup()

    def test_approval_import_update_and_delete_only_index_entries(self):
        first = self.folder / "first.txt"
        second = self.folder / "second.md"
        first.write_text("peach launch", encoding="utf-8")
        second.write_text("orange notes", encoding="utf-8")
        pending = self.assistant.process_text("sync knowledge folder Documents/library")
        self.assertTrue(pending.needs_confirmation)
        self.assertEqual(self.assistant.knowledge.search("peach"), [])
        self.assertTrue(self.assistant.resume(pending.action).success)
        first.write_text("plum review", encoding="utf-8")
        second.unlink()
        inventory = self.assistant.process_text("list knowledge")
        self.assertIn("changed:", inventory.text)
        self.assertIn("missing:", inventory.text)
        pending = self.assistant.process_text("sync knowledge folder Documents/library")
        result = self.assistant.resume(pending.action)
        self.assertTrue(result.success, result.text)
        self.assertEqual(len(result.data["removed"]), 1)
        self.assertTrue(first.exists())
        self.assertFalse(self.assistant.knowledge.search("peach"))
        self.assertTrue(self.assistant.knowledge.search("plum"))
        self.assertEqual(self.assistant.database.fetch_one("SELECT COUNT(*) AS n FROM knowledge_documents")["n"], 1)

    def test_partial_import_failure_does_not_remove_missing_entries(self):
        old = self.folder / "old.txt"
        old.write_text("old source", encoding="utf-8")
        self.assistant.knowledge.index(str(old))
        old.unlink()
        (self.folder / "bad.docx").write_text("not an archive", encoding="utf-8")
        pending = self.assistant.process_text("sync knowledge folder Documents/library")
        result = self.assistant.resume(pending.action)
        self.assertFalse(result.success)
        self.assertIn("No missing entries were removed", result.text)
        self.assertEqual(self.assistant.database.fetch_one("SELECT COUNT(*) AS n FROM knowledge_documents")["n"], 1)

    def test_folder_sync_never_indexes_symlinks_to_outside(self):
        outside = Path(self.tmp.name) / "outside.txt"
        outside.write_text("private orange text", encoding="utf-8")
        try:
            (self.folder / "link.txt").symlink_to(outside)
        except OSError:
            self.skipTest("symlinks unavailable")
        pending = self.assistant.process_text("sync knowledge folder Documents/library")
        result = self.assistant.resume(pending.action)
        self.assertTrue(result.success, result.text)
        self.assertEqual(self.assistant.knowledge.search("private"), [])
        self.assertTrue(outside.exists())

    def test_bounded_scan_has_no_partial_writes(self):
        for i in range(21):
            (self.folder / f"{i}.txt").write_text("test", encoding="utf-8")
        pending = self.assistant.process_text("index folder Documents/library")
        result = self.assistant.resume(pending.action)
        self.assertFalse(result.success)
        self.assertEqual(self.assistant.database.fetch_one("SELECT COUNT(*) AS n FROM knowledge_documents")["n"], 0)


if __name__ == "__main__":
    unittest.main()
