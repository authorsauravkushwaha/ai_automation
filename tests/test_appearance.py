"""Verify how the window *looks*, not merely that its slots fire.

The rest of the GUI suite proves behaviour: which handlers run, what state
changes. That is necessary and it is not sufficient — every one of those tests
passes just as happily against a window that is entirely unstyled, because a
stylesheet that never gets applied cannot break an assertion in them. This is
not hypothetical: until this file existed, ``MainWindow`` did not apply its own
theme, so the whole GUI suite was exercising default Qt widgets with a
fallback font and reporting success.

These tests render the real window offscreen and assert on what the user would
actually see: the theme is applied, the type scale survives, colours come from
the stylesheet instead of Qt defaults, regions do not overlap or clip, and the
pixels are not a blank sheet.
"""

from __future__ import annotations

import importlib.util
import os
import tempfile
import unittest

from tests.helpers import make_assistant

GUI_IMPORT_ERROR = ""
try:
    if importlib.util.find_spec("PySide6") is None:
        raise ImportError("PySide6 optional dependency is not installed")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtGui import QFontDatabase, QFontMetrics
    from PySide6.QtWidgets import QApplication, QLabel

    from hinata.interface.main_window import MainWindow
    from hinata.interface.styles import STYLE_SHEET
except ImportError as exc:
    QApplication = None
    QLabel = None
    QFontDatabase = None
    QFontMetrics = None
    MainWindow = None
    STYLE_SHEET = ""
    GUI_IMPORT_ERROR = str(exc)


@unittest.skipIf(QApplication is None, GUI_IMPORT_ERROR)
class AppearanceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.application = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.assistant = make_assistant(self.temporary.name)
        # Note the deliberate absence of application.setStyleSheet(...) here:
        # the window must theme itself or these assertions fail.
        self.window = MainWindow(self.assistant)
        self.window.resize(1280, 860)
        self.window.show()
        self.application.processEvents()

    def tearDown(self) -> None:
        self.window.close()
        self.assistant.close()
        self.temporary.cleanup()
        self.application.processEvents()

    def _labels(self) -> list[QLabel]:
        return self.window.centralWidget().findChildren(QLabel)

    def _by_object_name(self, name: str) -> QLabel:
        matches = [label for label in self._labels() if label.objectName() == name]
        self.assertTrue(matches, f"no QLabel with objectName={name!r}")
        return matches[0]

    # -- the theme is actually on the window ------------------------------

    def test_window_applies_the_stylesheet_itself(self) -> None:
        self.assertEqual(self.window.styleSheet(), STYLE_SHEET)

    def test_theme_reaches_a_child_widget(self) -> None:
        """A child's effective style must reflect the sheet, not Qt defaults."""
        badge = self.window.connection_badge
        # #93f7d8 comes from QLabel#localBadge; default Qt text would be black.
        self.assertEqual(badge.palette().windowText().color().name(), "#93f7d8")

    # -- the type scale survives -----------------------------------------

    def test_brand_title_is_large_and_bold(self) -> None:
        brand = self._by_object_name("brand")
        info = brand.fontInfo()
        self.assertTrue(info.bold(), "the HINATA wordmark must be bold")
        # Compare against body copy; the window-level QWidget rule is 13px.
        self.assertGreater(info.pointSizeF(), 12, "wordmark must dominate the header")
        self.assertGreater(info.pixelSize() if info.pixelSize() > 0 else 0, 0)

    def test_type_scale_is_ordered(self) -> None:
        brand = self._by_object_name("brand").fontInfo().pointSizeF()
        subtitle = self._by_object_name("subtitle").fontInfo().pointSizeF()
        muted = self._by_object_name("muted").fontInfo().pointSizeF()
        self.assertGreater(brand, subtitle, "wordmark must outrank its tagline")
        # Subtitle and muted share one 11px rule by design; both must still be
        # smaller than the wordmark and larger than Qt's tiny default.
        self.assertEqual(subtitle, muted)
        self.assertGreater(subtitle, 7.0)

    def test_text_renders_with_resolved_metrics(self) -> None:
        """Every label must be laid out with a real font, not Qt's placeholder.

        Qt reports the *resolved* family here, so this does not prove the sheet's
        preferred family exists on this box (on Linux it usually does not, and
        the family chain in styles.py is what makes that fallback deliberate
        rather than accidental). What it does prove is that text is measured
        against a concrete family with usable metrics rather than an unresolved
        generic, which is the failure mode that silently collapses spacing.
        """
        available = set(QFontDatabase.families())
        for name in ("brand", "subtitle", "muted"):
            info = self._by_object_name(name).fontInfo()
            with self.subTest(label=name):
                self.assertIn(info.family(), available)
                self.assertGreater(info.pixelSize(), 0)

    # -- regions are laid out, not overlapping ---------------------------

    def test_no_region_overlaps_the_status_bar(self) -> None:
        status_top = self.window.statusBar().geometry().y()
        footer_text = self.window.model_label
        self.assertLessEqual(
            footer_text.geometry().y() + footer_text.geometry().height(),
            status_top,
            "footer text is underneath the status bar",
        )

    def test_footer_text_is_not_clipped(self) -> None:
        label = self.window.model_label
        metrics = QFontMetrics(label.font())
        self.assertLessEqual(metrics.horizontalAdvance(label.text()), label.width())
        self.assertLessEqual(metrics.height(), label.height())

    def test_conversation_dominates_the_window(self) -> None:
        """The transcript is the point of the window; it must get the space."""
        window_area = self.window.width() * self.window.height()
        chat = self.window.conversation.geometry()
        self.assertGreater(chat.width() * chat.height(), window_area * 0.35)

    def test_input_row_is_below_the_transcript(self) -> None:
        chat_bottom = self.window.conversation.geometry().y() + self.window.conversation.geometry().height()
        self.assertGreater(self.window.input.geometry().y(), chat_bottom)

    def test_widgets_fit_inside_the_window(self) -> None:
        for name in ("input", "send_button", "model_label", "connection_badge"):
            widget = getattr(self.window, name)
            geometry = widget.geometry()
            with self.subTest(widget=name):
                self.assertGreaterEqual(geometry.x(), 0)
                self.assertGreaterEqual(geometry.y(), 0)
                self.assertLessEqual(geometry.x() + geometry.width(), self.window.width())
                self.assertLessEqual(geometry.y() + geometry.height(), self.window.height())

    # -- the render is real pixels, not a blank sheet --------------------

    def test_render_is_not_blank(self) -> None:
        image = self.window.grab().toImage()
        self.assertEqual((image.width(), image.height()), (self.window.width(), self.window.height()))
        colours = {
            image.pixelColor(x, y).name()
            for x in range(0, image.width(), 37)
            for y in range(0, image.height(), 31)
        }
        self.assertGreater(len(colours), 12, f"only {len(colours)} distinct colours: likely unpainted")

    def test_backdrop_and_panels_have_distinct_colours(self) -> None:
        """The glass header must sit visually above the painted backdrop."""
        image = self.window.grab().toImage()
        header = image.pixelColor(self.window.width() // 2, 30).name()
        backdrop = image.pixelColor(4, self.window.height() - 40).name()
        self.assertNotEqual(header, backdrop)
        # Canvas is near-black by design; panels are lifted off it.
        self.assertLess(sum(int(backdrop[i : i + 2], 16) for i in (1, 3, 5)), 60)
        self.assertGreater(sum(int(header[i : i + 2], 16) for i in (1, 3, 5)), 40)

    def test_primary_button_is_visibly_accented(self) -> None:
        """Send is the primary action and must not look like every other button.

        The theme styles by objectName, so "looks different" means the rendered
        pixels differ — not that an inline stylesheet string differs.
        """
        send = self.window.send_button
        secondary = [
            button for button in self.window.findChildren(type(send))
            if button is not send and button.isVisible() and button.width() > 60
        ]
        self.assertTrue(secondary, "expected sibling buttons to compare against")
        primary = send.grab().toImage()
        other = secondary[0].grab().toImage()
        self.assertGreater(primary.width(), 0)
        self.assertGreater(other.width(), 0)
        centre_x, centre_y = primary.width() // 2, primary.height() // 2
        self.assertNotEqual(
            primary.pixelColor(centre_x, centre_y).name(),
            other.pixelColor(other.width() // 2, other.height() // 2).name(),
            "the primary action is visually indistinguishable from secondary ones",
        )


if __name__ == "__main__":
    unittest.main()
