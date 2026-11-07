"""Single dark theme; no remote fonts, images, or telemetry."""

STYLE_SHEET = """
QWidget { background: #0b111b; color: #dce7f2; font-family: "Segoe UI"; font-size: 13px; }
QMainWindow, QDialog { background: #0b111b; }
QFrame#header, QFrame#panel { background: #111b29; border: 1px solid #1e2c3d; border-radius: 12px; }
QLabel#brand { font-size: 25px; font-weight: 700; color: #f1f7fb; }
QLabel#subtitle, QLabel#muted { color: #8295aa; font-size: 11px; }
QLabel#localBadge { color: #77dfc0; background: #12332f; border: 1px solid #1d5046; border-radius: 8px; padding: 4px 9px; font-size: 10px; font-weight: 600; }
QLabel#section { font-size: 15px; font-weight: 600; color: #edf5fb; }
QTextBrowser, QPlainTextEdit, QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox {
    background: #0e1723; border: 1px solid #26364a; border-radius: 8px; padding: 8px; selection-background-color: #24546d;
}
QTextBrowser#conversation { background: #0e1723; border: 1px solid #1d2c3e; border-radius: 12px; padding: 14px; font-size: 13px; }
QLineEdit:focus, QComboBox:focus { border: 1px solid #4cb9df; }
QPushButton { background: #172536; border: 1px solid #2a3d52; border-radius: 8px; padding: 8px 13px; color: #e5eff7; }
QPushButton:hover { background: #1e3146; border-color: #3c5872; }
QPushButton:pressed { background: #10202e; }
QPushButton:disabled { color: #65778a; background: #111b26; border-color: #1c2a38; }
QPushButton#primary { background: #16779c; border-color: #2595ba; color: white; font-weight: 600; }
QPushButton#primary:hover { background: #1a89af; }
QPushButton#recording { background: #7b3038; border-color: #ad4b55; }
QPushButton#danger { background: #552831; border-color: #813844; color: #ffdbe0; }
QTabWidget::pane { border: 1px solid #243448; border-radius: 8px; }
QTabBar::tab { background: #111c29; padding: 9px 15px; border: 1px solid #243448; }
QTabBar::tab:selected { background: #1b3244; color: #78d8f7; }
QScrollBar:vertical { background: #0e1723; width: 10px; margin: 0; }
QScrollBar::handle:vertical { background: #2b4055; min-height: 30px; border-radius: 5px; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QCheckBox { spacing: 8px; }
QToolTip { background: #162536; color: #e5eff7; border: 1px solid #30485f; padding: 5px; }
"""
