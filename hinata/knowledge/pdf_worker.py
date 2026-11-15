"""Isolated-process PDF text extraction. Invoked with a bounded stdin payload."""
from __future__ import annotations

import io
import sys

MAX_CHARS = 200_000
MAX_PAGES = 100


def main() -> int:
    # POSIX resource limits are defense in depth, not an OS sandbox. Windows has
    # only the caller's wall-clock timeout; the parser still processes untrusted data.
    try:
        import resource
        resource.setrlimit(resource.RLIMIT_AS, (512 * 1024 * 1024, 512 * 1024 * 1024))
        resource.setrlimit(resource.RLIMIT_CPU, (10, 10))
    except (ImportError, OSError, ValueError):
        pass
    try:
        from pypdf import PdfReader
    except ImportError:
        return 2
    try:
        reader = PdfReader(io.BytesIO(sys.stdin.buffer.read()), strict=True)
        if reader.is_encrypted:
            return 3
        if len(reader.pages) > MAX_PAGES:
            return 4
        texts = []
        count = 0
        for page_num, page in enumerate(reader.pages, 1):
            text = page.extract_text() or ""
            tagged = f"[[PAGE:{page_num}]]\n{text}" if text.strip() else ""
            if tagged:
                count += len(tagged) + 1
                if count > MAX_CHARS:
                    return 5
                texts.append(tagged)
        sys.stdout.buffer.write("\n".join(texts).encode("utf-8"))
        return 0
    except Exception:
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
