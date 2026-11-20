"""Reusable Qt widgets and bounded background-task signals."""

from __future__ import annotations

import html
import logging
import math
from collections.abc import Callable
from datetime import datetime
from typing import Any

from PySide6.QtCore import QObject, QRunnable, Qt, QTimer, Signal, Slot
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QTextBrowser, QVBoxLayout

LOGGER = logging.getLogger(__name__)


def _blend(dim: QColor, target: QColor, amount: float) -> str:
    """Interpolate two colours; ``amount`` 0 keeps dim, 1 keeps target."""
    red = round(dim.red() + (target.red() - dim.red()) * amount)
    green = round(dim.green() + (target.green() - dim.green()) * amount)
    blue = round(dim.blue() + (target.blue() - dim.blue()) * amount)
    return f"#{red:02x}{green:02x}{blue:02x}"


class StatusPill(QFrame):
    """LED-style status indicator whose active dot breathes with a soft pulse."""

    DIM = "#53677b"

    def __init__(self, title: str) -> None:
        super().__init__()
        self.setObjectName("panel")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(9, 5, 9, 5)
        layout.setSpacing(6)
        self.dot = QLabel("●")
        self.dot.setStyleSheet(f"color: {self.DIM};")
        self.label = QLabel(title)
        self.label.setObjectName("muted")
        layout.addWidget(self.dot)
        layout.addWidget(self.label)
        self._active = False
        self._target = QColor("#62d6b1")
        self._phase = 0.0
        self._timer = QTimer(self)
        self._timer.setInterval(55)
        self._timer.timeout.connect(self._pulse)

    def set_active(self, active: bool, color: str = "#62d6b1") -> None:
        self._active = active
        self._target = QColor(color)
        self._phase = 0.0
        if active:
            self.dot.setStyleSheet(f"color: {color};")
            self._timer.start()
        else:
            self._timer.stop()
            self.dot.setStyleSheet(f"color: {self.DIM};")

    def _pulse(self) -> None:
        self._phase += 0.3
        amount = 0.62 + 0.38 * math.sin(self._phase)
        if not self._active:
            self._timer.stop()
            return
        self.dot.setStyleSheet(f"color: {_blend(QColor(self.DIM), self._target, amount)};")


class ConversationView(QTextBrowser):
    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("conversation")
        self.setReadOnly(True)
        self.setOpenExternalLinks(False)
        self.setOpenLinks(False)
        self.setAcceptRichText(True)
        self.setPlaceholderText("Your local conversation will appear here…")
        self.document().setMaximumBlockCount(300)

    @Slot(str, str)
    def add_message(self, role: str, text: str) -> None:
        is_user = role == "You"
        color = "#78d8f7" if is_user else "#77dfc0"
        safe_role = html.escape(role)
        safe_text = html.escape(text).replace("\n", "<br>")
        timestamp = datetime.now().strftime("%H:%M")
        scrollbar = self.verticalScrollBar()
        position = scrollbar.value()
        following = position == scrollbar.maximum()
        self.append(
            f'<div style="margin:8px 0 2px 0;color:{color};font-weight:600">'
            f'{safe_role} <span style="color:#64798d;font-weight:400">{timestamp}</span></div>'
            f'<div style="margin:0 0 12px 0;color:#dce7f2;line-height:145%">{safe_text}</div>'
        )
        scrollbar.setValue(scrollbar.maximum() if following else position)

    def scroll_history(self, pages: int) -> None:
        scrollbar = self.verticalScrollBar()
        scrollbar.setValue(scrollbar.value() + scrollbar.pageStep() * pages)

    @Slot()
    def jump_to_latest(self) -> None:
        scrollbar = self.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())


class WorkerSignals(QObject):
    result = Signal(object)
    error = Signal(str)
    finished = Signal()


class Worker(QRunnable):
    """Run inference/file work outside the UI thread; never touches Qt widgets."""

    def __init__(self, function: Callable[..., Any], *args: Any, **kwargs: Any) -> None:
        super().__init__()
        self.function = function
        self.args = args
        self.kwargs = kwargs
        self.signals = WorkerSignals()

    @Slot()
    def run(self) -> None:
        try:
            value = self.function(*self.args, **self.kwargs)
        except Exception as exc:
            LOGGER.exception("Background desktop task failed")
            self.signals.error.emit(str(exc))
        else:
            self.signals.result.emit(value)
        finally:
            self.signals.finished.emit()


class LabeledSection(QFrame):
    def __init__(self, title: str) -> None:
        super().__init__()
        self.setObjectName("panel")
        self.section_layout = QVBoxLayout(self)
        label = QLabel(title)
        label.setObjectName("section")
        self.section_layout.addWidget(label, alignment=Qt.AlignmentFlag.AlignLeft)
