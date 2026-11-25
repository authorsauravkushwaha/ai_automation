"""Dark glass-and-depth theme; no remote fonts, images, or telemetry.

The window backdrop (perspective grid, glowing orbs) is painted by
``SceneCanvas`` in ``hinata.interface.canvas``; these rules keep every panel
translucent so the 3D scene shows through, while buttons and inputs use
vertical gradients plus a press-down padding shift for a tactile feel.
"""

from pathlib import Path

_ARROW_IMAGE = (Path(__file__).resolve().parent / "assets" / "down-chevron.png").as_posix()
STYLE_SHEET = """
/* Base stays transparent so SceneCanvas is visible behind every child. */
/* Segoe UI first (native Windows look), then the closest well-hinted
   families on Linux/macOS. Without the chain Qt silently picks a
   proportional default whose metrics break the header spacing. */
QWidget {
    color: #dce7f2;
    font-family: "Segoe UI", "Inter", "Noto Sans", "DejaVu Sans", sans-serif;
    font-size: 13px;
}
QWidget#canvas { background: transparent; }
QMainWindow { background: #050810; }
QDialog {
    background: qlineargradient(x1: 0, y1: 0, x2: 0, y2: 1,
        stop: 0 #0e1a30, stop: 1 #070d18);
}

/* Floating glass panels over the animated backdrop. */
QFrame#header {
    background: qlineargradient(x1: 0, y1: 0, x2: 1, y2: 1,
        stop: 0 rgba(24, 42, 66, 0.93), stop: 0.55 rgba(14, 27, 46, 0.88),
        stop: 1 rgba(17, 48, 64, 0.91));
    border: 1px solid rgba(92, 180, 226, 0.38);
    border-radius: 14px;
}
QFrame#panel {
    background: rgba(13, 25, 41, 0.80);
    border: 1px solid rgba(78, 140, 182, 0.34);
    border-radius: 12px;
}
QLabel#brand { font-size: 26px; font-weight: 700; color: #f6fcff; }
QLabel#subtitle, QLabel#muted { color: #93abc2; font-size: 11px; }
QLabel#localBadge {
    color: #93f7d8;
    background: rgba(13, 56, 47, 0.88);
    border: 1px solid #33a585;
    border-radius: 10px;
    padding: 5px 11px;
    font-size: 10px;
    font-weight: 600;
}
QLabel#section { font-size: 15px; font-weight: 600; color: #edf5fb; }

/* Input surfaces. */
QTextBrowser, QPlainTextEdit, QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox {
    background: rgba(9, 18, 30, 0.93);
    border: 1px solid rgba(68, 118, 158, 0.55);
    border-radius: 10px;
    padding: 8px 10px;
    selection-background-color: #27638a;
    selection-color: #eaf6ff;
}
QTextBrowser#conversation {
    background: qlineargradient(x1: 0, y1: 0, x2: 0, y2: 1,
        stop: 0 rgba(11, 21, 36, 0.95), stop: 1 rgba(8, 15, 27, 0.91));
    border: 1px solid rgba(88, 176, 224, 0.38);
    border-radius: 14px;
    padding: 16px;
    font-size: 13px;
}
QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus,
QPlainTextEdit:focus {
    border: 1px solid #4fd8ff;
    background: rgba(13, 29, 47, 0.96);
}

/* Buttons read as extruded 3D surfaces: lit top, dark base, press sinks in. */
QPushButton {
    background: qlineargradient(x1: 0, y1: 0, x2: 0, y2: 1,
        stop: 0 #2e4666, stop: 0.5 #1d3149, stop: 1 #14233a);
    border: 1px solid #406083;
    border-radius: 9px;
    padding: 8px 15px;
    color: #e9f3fb;
}
QPushButton:hover {
    background: qlineargradient(x1: 0, y1: 0, x2: 0, y2: 1,
        stop: 0 #3d5c82, stop: 0.5 #28425f, stop: 1 #1a2e49);
    border-color: #628cb8;
}
QPushButton:pressed {
    background: qlineargradient(x1: 0, y1: 0, x2: 0, y2: 1,
        stop: 0 #0e1a2b, stop: 1 #1b2f49);
    padding-top: 10px;
    padding-bottom: 6px;
}
QPushButton:focus { border-color: #4fd8ff; }
QPushButton:disabled { color: #67798d; background: #131d2a; border-color: #1f2e3f; }
QPushButton#primary {
    background: qlineargradient(x1: 0, y1: 0, x2: 0, y2: 1,
        stop: 0 #4ad6ff, stop: 0.45 #1e9fd2, stop: 1 #0c5e82);
    border: 1px solid #86e9ff;
    color: #ffffff;
    font-weight: 600;
}
QPushButton#primary:hover {
    background: qlineargradient(x1: 0, y1: 0, x2: 0, y2: 1,
        stop: 0 #6fe2ff, stop: 0.45 #2ab0e4, stop: 1 #0f6c95);
}
QPushButton#primary:pressed {
    background: qlineargradient(x1: 0, y1: 0, x2: 0, y2: 1,
        stop: 0 #0c4a68, stop: 1 #17799f);
    padding-top: 10px;
    padding-bottom: 6px;
}
QPushButton#recording {
    background: qlineargradient(x1: 0, y1: 0, x2: 0, y2: 1,
        stop: 0 #e05a68, stop: 1 #7c2f39);
    border-color: #ff8f9a;
    color: #ffffff;
    font-weight: 600;
}
QPushButton#recording:pressed { padding-top: 10px; padding-bottom: 6px; }
QPushButton#danger {
    background: qlineargradient(x1: 0, y1: 0, x2: 0, y2: 1,
        stop: 0 #6d3340, stop: 1 #45222c);
    border-color: #9a4653;
    color: #ffdbe0;
}
QPushButton#danger:hover {
    background: qlineargradient(x1: 0, y1: 0, x2: 0, y2: 1,
        stop: 0 #84404f, stop: 1 #552933);
}

/* Tabs. */
QTabWidget::pane {
    border: 1px solid rgba(78, 140, 182, 0.42);
    border-radius: 10px;
    background: rgba(10, 19, 33, 0.92);
    top: -1px;
}
QTabBar::tab {
    background: rgba(14, 26, 43, 0.88);
    color: #9db4c9;
    padding: 9px 16px;
    border: 1px solid rgba(68, 118, 158, 0.4);
    border-top-left-radius: 8px;
    border-top-right-radius: 8px;
    margin-right: 4px;
}
QTabBar::tab:selected {
    background: qlineargradient(x1: 0, y1: 0, x2: 0, y2: 1,
        stop: 0 #1d4f70, stop: 1 #14324b);
    color: #8fe9ff;
    border-color: rgba(126, 220, 250, 0.6);
}

/* Scrollbars. */
QScrollBar:vertical {
    background: rgba(9, 17, 29, 0.85);
    width: 11px;
    margin: 0;
    border-radius: 5px;
}
QScrollBar::handle:vertical {
    background: qlineargradient(x1: 0, y1: 0, x2: 0, y2: 1,
        stop: 0 #3f6386, stop: 1 #27405c);
    min-height: 34px;
    border-radius: 5px;
}
QScrollBar::handle:vertical:hover { background: #4d7ba6; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; width: 0; }
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: transparent; }
QScrollBar:horizontal {
    background: rgba(9, 17, 29, 0.85);
    height: 11px;
    border-radius: 5px;
}
QScrollBar::handle:horizontal {
    background: #33516e;
    min-width: 34px;
    border-radius: 5px;
}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal { width: 0; height: 0; }

/* Checkboxes and combo boxes. */
QCheckBox { spacing: 8px; }
QCheckBox::indicator {
    width: 15px;
    height: 15px;
    border: 1px solid #4a7ea8;
    border-radius: 4px;
    background: rgba(9, 18, 30, 0.95);
}
QCheckBox::indicator:checked {
    background: qlineargradient(x1: 0, y1: 0, x2: 0, y2: 1,
        stop: 0 #45d4ff, stop: 1 #0c5e82);
    border-color: #86e9ff;
}
QCheckBox::indicator:unchecked:hover { border-color: #4fd8ff; }
QComboBox::drop-down {
    subcontrol-origin: padding;
    subcontrol-position: center right;
    width: 28px;
    background: transparent;
    border: none;
}
QComboBox::down-arrow {
    image: url("__HINATA_ARROW_IMAGE__");
    width: 18px;
    height: 12px;
}
QComboBox QAbstractItemView {
    background: #0e1a2c;
    border: 1px solid #2c4863;
    color: #dce7f2;
    selection-background-color: #1d6f96;
    selection-color: #ffffff;
    outline: none;
}

/* Status bar and menus. */
QStatusBar {
    background: rgba(6, 11, 19, 0.94);
    color: #93abc2;
    border-top: 1px solid rgba(68, 118, 158, 0.32);
    padding: 2px 8px;
}
QStatusBar::item { background: transparent; }
QMenuBar { background: transparent; color: #dce7f2; }
QMenuBar::item:selected { background: rgba(29, 111, 150, 0.85); }
QMenu {
    background: #0e1a2c;
    border: 1px solid #2c4863;
    color: #dce7f2;
    padding: 4px;
}
QMenu::item { padding: 6px 24px 6px 12px; border-radius: 5px; }
QMenu::item:selected { background: #1d6f96; }
QMenu::separator { height: 1px; background: #24405c; margin: 4px 6px; }

QToolTip {
    background: #16283c;
    color: #e5eff7;
    border: 1px solid #3a5878;
    padding: 6px 8px;
    border-radius: 6px;
}
""".replace("__HINATA_ARROW_IMAGE__", _ARROW_IMAGE)
