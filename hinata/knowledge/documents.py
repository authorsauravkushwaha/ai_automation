"""Bounded offline document extraction. No document content is executed."""
from __future__ import annotations

import csv
import io
import json
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
        content = str(data["content"])
        if suffix == ".csv":
            return _format_csv(content)
        if suffix == ".json":
            return _format_json(content)
        return content
    try:
        with path.open("rb") as handle:
            raw = handle.read(files.config.max_read_bytes + 1)
    except OSError as exc:
        raise AutomationError(f"Could not read document: {exc}") from exc
    if len(raw) > files.config.max_read_bytes:
        raise AutomationError("Document exceeds the configured byte limit; no changes were made")
    return _extract_pdf(raw) if suffix == ".pdf" else _extract_docx(raw)


def _format_csv(raw_text: str) -> str:
    try:
        reader = csv.reader(io.StringIO(raw_text))
        rows = list(reader)
        if not rows:
            return ""
        header = rows[0]
        formatted = ["# CSV Table: " + " | ".join(col.strip() for col in header)]
        for row_idx, row in enumerate(rows[1:], 2):
            pairs = [
                f"{header[i].strip() if i < len(header) and header[i].strip() else f'col_{i+1}'}: {val.strip()}"
                for i, val in enumerate(row)
                if val.strip()
            ]
            if pairs:
                formatted.append(f"Row {row_idx}: " + "; ".join(pairs))
        out = "\n".join(formatted)
        return out[:MAX_CHARS]
    except csv.Error:
        return raw_text


def _format_json(raw_text: str) -> str:
    try:
        parsed = json.loads(raw_text)
        pretty = json.dumps(parsed, indent=2, ensure_ascii=False, sort_keys=True)
        return pretty[:MAX_CHARS]
    except (json.JSONDecodeError, TypeError, ValueError):
        return raw_text


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
        body = root.find(ns + "body")
        container = body if body is not None else root
        lines: list[str] = []
        count = 0

        def _paragraph_text(elem: ET.Element) -> str:
            parts: list[str] = []
            for item in elem.iter():
                if item.tag == ns + "t":
                    parts.append(item.text or "")
                elif item.tag == ns + "tab":
                    parts.append("\t")
                elif item.tag in {ns + "br", ns + "cr"}:
                    parts.append("\n")
            return "".join(parts)

        for child in container:
            if child.tag == ns + "p":
                p_style = child.find(f"{ns}pPr/{ns}pStyle")
                style_val = p_style.attrib.get(ns + "val", "") if p_style is not None else ""
                text = _paragraph_text(child)
                if style_val.lower().startswith("heading") and text.strip():
                    text = f"# {text}"
                count += len(text) + 1
                if count > MAX_CHARS:
                    raise AutomationError("DOCX exceeds the extracted-text limit")
                lines.append(text)
            elif child.tag == ns + "tbl":
                for row_elem in child.findall(ns + "tr"):
                    cells = [
                        " ".join(_paragraph_text(p).strip() for p in tc.findall(ns + "p")).strip()
                        for tc in row_elem.findall(ns + "tc")
                    ]
                    row_text = " | ".join(cells)
                    count += len(row_text) + 1
                    if count > MAX_CHARS:
                        raise AutomationError("DOCX exceeds the extracted-text limit")
                    if row_text:
                        lines.append(row_text)
        return "\n".join(lines)
    except AutomationError:
        raise
    except (zipfile.BadZipFile, UnicodeDecodeError, ET.ParseError, OSError, RuntimeError, ValueError, NotImplementedError) as exc:
        raise AutomationError("DOCX could not be safely parsed") from exc
