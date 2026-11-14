"""Bounded offline document extraction. No document content is executed."""
from __future__ import annotations

import io
import subprocess
import sys
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

from hinata.automation.app_control import AutomationError
from hinata.automation.file_manager import FileManager

MAX_CHARS = 200_000
MAX_DOCX_XML_BYTES = 2_000_000
MAX_ARCHIVE_ENTRIES = 2048
PDF_TIMEOUT_SECONDS = 15


def extract_document(files: FileManager, path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix not in {".pdf", ".docx"}:
        data = files.read_text(str(path), MAX_CHARS)
        if data["truncated"]:
            raise AutomationError("Document exceeds the knowledge import limit; no changes were made")
        return data["content"]
    try:
        with path.open("rb") as handle:
            raw = handle.read(files.config.max_read_bytes + 1)
    except OSError as exc:
        raise AutomationError(f"Could not read document: {exc}") from exc
    if len(raw) > files.config.max_read_bytes:
        raise AutomationError("Document exceeds the configured byte limit; no changes were made")
    return _extract_pdf(raw) if suffix == ".pdf" else _extract_docx(raw)


def _extract_pdf(raw: bytes) -> str:
    worker = Path(__file__).with_name("pdf_worker.py")
    try:
        result = subprocess.run([sys.executable, "-I", str(worker)], input=raw,
                                capture_output=True, timeout=PDF_TIMEOUT_SECONDS, check=False)
    except subprocess.TimeoutExpired as exc:
        raise AutomationError("PDF extraction timed out; no changes were made") from exc
    except OSError as exc:
        raise AutomationError(f"Could not start local PDF extractor: {exc}") from exc
    errors = {
        1: "PDF could not be parsed",
        2: 'PDF support requires pypdf; install HINATA with the "documents" extra during setup',
        3: "Encrypted PDFs are not supported",
        4: "PDF exceeds the 100-page limit",
        5: "PDF exceeds the extracted-text limit",
    }
    if result.returncode:
        raise AutomationError(errors.get(result.returncode, "PDF extraction failed or exceeded resource limits"))
    try:
        return result.stdout.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise AutomationError("PDF extractor produced invalid text") from exc


def _extract_docx(raw: bytes) -> str:
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            entries = archive.infolist()
            if len(entries) > MAX_ARCHIVE_ENTRIES:
                raise AutomationError("DOCX contains too many archive entries")
            documents = [item for item in entries if item.filename == "word/document.xml"]
            if len(documents) != 1:
                raise AutomationError("DOCX must contain exactly one main document")
            document = documents[0]
            if document.file_size > MAX_DOCX_XML_BYTES or document.flag_bits & 1:
                raise AutomationError("DOCX main document is too large or encrypted")
            with archive.open(document) as stream:
                xml_bytes = stream.read(MAX_DOCX_XML_BYTES + 1)
            if len(xml_bytes) > MAX_DOCX_XML_BYTES:
                raise AutomationError("DOCX main document exceeds the extraction limit")
        # UTF-8 only avoids alternate encodings hiding XML entity declarations.
        xml = xml_bytes.decode("utf-8-sig")
        if "\x00" in xml or "<!DOCTYPE" in xml.upper() or "<!ENTITY" in xml.upper():
            raise AutomationError("DOCX XML declarations are not permitted")
        root = ET.fromstring(xml)
        ns = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
        paragraphs = []
        count = 0
        for paragraph in root.iter(ns + "p"):
            parts = []
            for item in paragraph.iter():
                if item.tag == ns + "t":
                    parts.append(item.text or "")
                elif item.tag == ns + "tab":
                    parts.append("\t")
                elif item.tag in {ns + "br", ns + "cr"}:
                    parts.append("\n")
            text = "".join(parts)
            count += len(text) + 1
            if count > MAX_CHARS:
                raise AutomationError("DOCX exceeds the extracted-text limit")
            paragraphs.append(text)
        return "\n".join(paragraphs)
    except AutomationError:
        raise
    except (zipfile.BadZipFile, UnicodeDecodeError, ET.ParseError, OSError, RuntimeError, ValueError, NotImplementedError) as exc:
        raise AutomationError("DOCX could not be safely parsed") from exc
