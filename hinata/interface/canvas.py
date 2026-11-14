"""Locally painted 3D backdrop and glossy logo orb for the desktop window.

Everything here is drawn with QPainter at runtime: no external images, no
network access, and the animation throttles itself to ~25 fps (and stops
entirely while the window is hidden) so a 4 GB laptop is not taxed.
"""

from __future__ import annotations

import math
import random

from PySide6.QtCore import QPointF, QRectF, Qt, QTimer
from PySide6.QtGui import (
    QBrush,
    QColor,
    QLinearGradient,
    QPainter,
    QPen,
    QRadialGradient,
)
from PySide6.QtWidgets import QWidget

# Floating glow orbs: (x, y, radius, r, g, b, drift, speed, phase) as fractions
# of the window so the scene scales with the desktop it is shown on.
_ORBS = (
    (0.12, 0.28, 96, 56, 180, 246, 12, 0.40, 0.0),
    (0.87, 0.22, 128, 132, 92, 250, 16, 0.30, 2.1),
    (0.74, 0.74, 74, 45, 212, 182, 9, 0.52, 4.0),
    (0.28, 0.82, 58, 92, 160, 255, 7, 0.61, 1.2),
    (0.55, 0.14, 48, 64, 222, 236, 10, 0.45, 3.3),
)


def _build_stars(count: int = 64) -> tuple[tuple[float, float, float, float], ...]:
    """Deterministic star field above the horizon: (x, y, size, twinkle phase)."""
    generator = random.Random(7)
    stars = []
    for _ in range(count):
        stars.append(
            (
                generator.random(),
                generator.random() * 0.58,
                generator.uniform(0.6, 1.9),
                generator.uniform(0.0, math.tau),
            )
        )
    return tuple(stars)


class SceneCanvas(QWidget):
    """Deep-space gradient, receding 3D floor grid, orbs, stars, and vignette."""

    FRAME_MS = 40  # ~25 fps: calm to watch and cheap to paint.

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("canvas")
        # We paint every pixel of the frame ourselves each tick.
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, True)
        self._stars = _build_stars()
        self._phase = 0.0
        self._time = 0.0
        self._timer = QTimer(self)
        self._timer.setInterval(self.FRAME_MS)
        self._timer.timeout.connect(self._advance)
        self._timer.start()

    # ------------------------------------------------------------- animation
    def _advance(self) -> None:
        self._phase = (self._phase + 0.007) % 1.0
        self._time += 0.04
        self.update()

    def showEvent(self, event: object) -> None:  # noqa: D401 - Qt callback
        super().showEvent(event)  # type: ignore[arg-type]
        if not self._timer.isActive():
            self._timer.start()

    def hideEvent(self, event: object) -> None:
        super().hideEvent(event)  # type: ignore[arg-type]
        self._timer.stop()  # Never burn CPU while minimised or closing.

    # --------------------------------------------------------------- painting
    def paintEvent(self, event: object) -> None:
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            width = self.width()
            height = self.height()
            if width <= 0 or height <= 0:
                return
            self._paint_sky(painter, width, height)
            horizon = height * 0.58
            self._paint_horizon_glow(painter, width, height, horizon)
            self._paint_floor(painter, width, height, horizon)
            self._paint_stars(painter, width, height, horizon)
            self._paint_orbs(painter, width, height)
            self._paint_vignette(painter, width, height)
        finally:
            painter.end()

    def _paint_sky(self, painter: QPainter, width: int, height: int) -> None:
        sky = QLinearGradient(0, 0, 0, height)
        sky.setColorAt(0.0, QColor("#04070d"))
        sky.setColorAt(0.42, QColor("#0a1526"))
        sky.setColorAt(0.58, QColor("#0c1c33"))
        sky.setColorAt(1.0, QColor("#050810"))
        painter.fillRect(0, 0, width, height, sky)

    def _paint_horizon_glow(
        self, painter: QPainter, width: int, height: int, horizon: float
    ) -> None:
        radius = max(width, height) * 0.55
        glow = QRadialGradient(QPointF(width / 2, horizon), radius)
        glow.setColorAt(0.0, QColor(58, 190, 248, 64))
        glow.setColorAt(0.35, QColor(34, 124, 196, 30))
        glow.setColorAt(1.0, QColor(0, 0, 0, 0))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QBrush(glow))
        painter.drawEllipse(QPointF(width / 2, horizon), radius, radius)

    def _paint_floor(
        self, painter: QPainter, width: int, height: int, horizon: float
    ) -> None:
        """Perspective floor: converging rails plus rows drifting toward you."""
        painter.save()
        painter.setClipRect(0, int(horizon), width, height - int(horizon))
        center = QPointF(width / 2, horizon)
        span = 14
        floor_reach = height - horizon
        for index in range(-span, span + 1):
            bottom = QPointF(width / 2 + index * (width / span) * 0.9, height + 24)
            edge = abs(index) / span
            alpha = int(30 + 70 * (1.0 - edge * 0.6))
            rail = QLinearGradient(center, bottom)
            rail.setColorAt(0.0, QColor(94, 203, 255, 0))
            rail.setColorAt(0.3, QColor(94, 203, 255, int(alpha * 0.35)))
            rail.setColorAt(1.0, QColor(126, 224, 255, alpha))
            painter.setPen(QPen(QBrush(rail), 1.3))
            painter.drawLine(center, bottom)

        rows = 16
        for row in range(rows):
            depth = row + self._phase  # Continuous across the 0..1 wrap.
            spread = 1.0 - 1.0 / (1.0 + depth * 0.45)
            y = horizon + spread * floor_reach * 1.25
            if y > height + 2:
                continue
            fade = min(1.0, spread * 2.4)
            alpha = int(20 + 95 * fade)
            painter.setPen(QPen(QColor(104, 212, 255, alpha), 1.0))
            painter.drawLine(0, int(y), width, int(y))
        painter.restore()

    def _paint_stars(
        self, painter: QPainter, width: int, height: int, horizon: float
    ) -> None:
        painter.setPen(Qt.PenStyle.NoPen)
        for x_frac, y_frac, size, phase in self._stars:
            twinkle = 0.55 + 0.45 * math.sin(self._time * 1.7 + phase)
            alpha = int(40 + 120 * twinkle)
            painter.setBrush(QColor(214, 236, 255, alpha))
            x = x_frac * width
            y = y_frac * horizon
            painter.drawEllipse(QPointF(x, y), size, size)

    def _paint_orbs(self, painter: QPainter, width: int, height: int) -> None:
        scale = 0.55 + min(1.0, width / 1400.0) * 0.6
        painter.setPen(Qt.PenStyle.NoPen)
        for x_frac, y_frac, radius, red, green, blue, drift, speed, phase in _ORBS:
            x = width * x_frac + math.sin(self._time * speed * 0.7 + phase) * 16
            y = height * y_frac + math.sin(self._time * speed + phase) * drift
            pulse = 0.78 + 0.22 * math.sin(self._time * speed * 1.6 + phase)
            size = radius * pulse * scale
            halo = QRadialGradient(QPointF(x, y), size)
            halo.setColorAt(0.0, QColor(red, green, blue, 118))
            halo.setColorAt(0.4, QColor(red, green, blue, 52))
            halo.setColorAt(1.0, QColor(red, green, blue, 0))
            painter.setBrush(QBrush(halo))
            painter.drawEllipse(QPointF(x, y), size, size)
            core = QRadialGradient(QPointF(x, y), max(4.0, size * 0.22))
            core.setColorAt(0.0, QColor(255, 255, 255, 130))
            core.setColorAt(1.0, QColor(red, green, blue, 0))
            painter.setBrush(QBrush(core))
            painter.drawEllipse(QPointF(x, y), max(4.0, size * 0.22), max(4.0, size * 0.22))

    def _paint_vignette(self, painter: QPainter, width: int, height: int) -> None:
        vignette = QRadialGradient(
            QPointF(width / 2, height / 2), max(width, height) * 0.78
        )
        vignette.setColorAt(0.0, QColor(0, 0, 0, 0))
        vignette.setColorAt(0.68, QColor(0, 0, 0, 0))
        vignette.setColorAt(1.0, QColor(2, 4, 9, 165))
        painter.fillRect(0, 0, width, height, vignette)


class OrbLogo(QWidget):
    """Glossy 3D sphere with a slow travelling sheen for the header."""

    SIZE = 38

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFixedSize(self.SIZE, self.SIZE)
        self._sheen = 0.0
        self._timer = QTimer(self)
        self._timer.setInterval(55)
        self._timer.timeout.connect(self._advance)
        self._timer.start()

    def _advance(self) -> None:
        self._sheen = (self._sheen + 0.05) % math.tau
        self.update()

    def hideEvent(self, event: object) -> None:
        super().hideEvent(event)  # type: ignore[arg-type]
        self._timer.stop()

    def showEvent(self, event: object) -> None:
        super().showEvent(event)  # type: ignore[arg-type]
        if not self._timer.isActive():
            self._timer.start()

    def paintEvent(self, event: object) -> None:
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            rect = QRectF(1, 1, self.SIZE - 2, self.SIZE - 2)
            center = rect.center()
            radius = rect.width() / 2

            # Outer cyan bloom.
            halo = QRadialGradient(center, radius * 1.75)
            halo.setColorAt(0.0, QColor(70, 200, 255, 90))
            halo.setColorAt(1.0, QColor(70, 200, 255, 0))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QBrush(halo))
            painter.drawEllipse(center, radius * 1.75, radius * 1.75)

            # Sphere body: lit from the upper left, dark navy at the rim.
            body = QRadialGradient(
                QPointF(center.x() - radius * 0.35, center.y() - radius * 0.4),
                radius * 1.6,
            )
            body.setColorAt(0.0, QColor("#b8f2ff"))
            body.setColorAt(0.22, QColor("#41c9f7"))
            body.setColorAt(0.55, QColor("#187fc0"))
            body.setColorAt(0.85, QColor("#0b4a7d"))
            body.setColorAt(1.0, QColor("#062a4d"))
            painter.setBrush(QBrush(body))
            painter.setPen(QPen(QColor(130, 226, 255, 160), 1.2))
            painter.drawEllipse(rect)

            # Specular highlight.
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(255, 255, 255, 205))
            painter.drawEllipse(
                QPointF(center.x() - radius * 0.38, center.y() - radius * 0.45),
                radius * 0.3,
                radius * 0.2,
            )
            painter.setBrush(QColor(255, 255, 255, 140))
            painter.drawEllipse(
                QPointF(center.x() - radius * 0.12, center.y() - radius * 0.62),
                radius * 0.12,
                radius * 0.1,
            )

            # Travelling sheen streak for the animated 3D feel.
            painter.save()
            painter.translate(center)
            painter.rotate(math.degrees(math.sin(self._sheen) * 38))
            streak = QLinearGradient(QPointF(-radius, 0.0), QPointF(radius, 0.0))
            streak.setColorAt(0.0, QColor(255, 255, 255, 0))
            streak.setColorAt(0.5, QColor(255, 255, 255, 58))
            streak.setColorAt(1.0, QColor(255, 255, 255, 0))
            painter.setBrush(QBrush(streak))
            painter.drawEllipse(QRectF(-radius * 0.9, -radius * 0.5, radius * 1.8, radius * 1.0))
            painter.restore()

            # Rim light on the lower-right edge.
            rim = QLinearGradient(rect.topLeft(), rect.bottomRight())
            rim.setColorAt(0.0, QColor(255, 255, 255, 0))
            rim.setColorAt(1.0, QColor(168, 240, 255, 120))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(QBrush(rim), 1.6))
            painter.drawEllipse(rect)
        finally:
            painter.end()
