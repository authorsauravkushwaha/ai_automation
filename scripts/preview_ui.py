"""Render the desktop window offscreen for visual review.

    python scripts/preview_ui.py            # writes ui-preview.png
    python scripts/preview_ui.py out.png

The window is built with a throwaway local assistant (temporary data
directory), a few conversation turns, and one tick of the animation timers so
the backdrop, glass panels, and 3D buttons appear exactly as a user sees them.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from hinata.interface.main_window import MainWindow  # noqa: E402
from hinata.interface.styles import STYLE_SHEET  # noqa: E402
from tests.helpers import make_assistant  # noqa: E402


def main() -> int:
    destination = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "ui-preview.png"
    application = QApplication.instance() or QApplication(sys.argv)
    application.setStyleSheet(STYLE_SHEET)
    with tempfile.TemporaryDirectory() as tmp:
        assistant = make_assistant(tmp)
        window = MainWindow(assistant)
        window.resize(1180, 780)
        window.conversation.add_message("You", "open notepad")
        window.conversation.add_message(
            "HINATA", "Notepad is open. Your file window is ready for typing."
        )
        window.conversation.add_message("You", "what is my system info?")
        window.conversation.add_message(
            "HINATA",
            "CPU: local quad-core, RAM profile tuned for 4 GB, storage healthy. "
            "Everything above ran on this PC only.",
        )
        window.connection_badge.setText("●  LOCAL ONLY")
        window.show()
        # Let a few animation frames paint before grabbing the window.
        deadline = QTimer()
        deadline.setSingleShot(True)
        deadline.timeout.connect(application.quit)
        deadline.start(700)
        application.exec()
        pixmap = window.grab()
        window.close()
        assistant.close()
    if not pixmap.save(str(destination)):
        print(f"Could not save {destination}", file=sys.stderr)
        return 1
    print(f"Saved {destination}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
