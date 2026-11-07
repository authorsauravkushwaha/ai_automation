"""Turn unittest failure blocks into bounded GitHub Actions annotations."""

from __future__ import annotations

import sys
from pathlib import Path


def escape_annotation(text: str) -> str:
    return text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def main() -> None:
    text = Path(sys.argv[1]).read_text(encoding="utf-8", errors="replace")
    sections = text.split("=" * 70)
    failures = [section for section in sections if "\nERROR:" in section or "\nFAIL:" in section]
    if not failures:
        failures = [text]
    for failure in failures:
        # Keep each annotation below GitHub's display limit and preserve the traceback tail.
        print("::error::" + escape_annotation(failure[-3500:]))


if __name__ == "__main__":
    main()
