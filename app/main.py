"""AssForge — ASS subtitle authoring tool. Application entry point."""
from __future__ import annotations

import os
import sys
import logging

# Windows 콘솔에서 한글 로그가 cp949 로 깨지는 것 방지
if sys.platform == "win32":
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        except Exception:
            pass

# Add project root to DLL search path so python-mpv can find libmpv-2.dll
_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ["PATH"] = _root + os.pathsep + os.environ.get("PATH", "")
if hasattr(os, "add_dll_directory"):
    os.add_dll_directory(_root)

from PySide6.QtWidgets import QApplication
from PySide6.QtCore import Qt
from PySide6.QtGui import QPalette, QColor


def setup_dark_theme(app: QApplication) -> None:
    """Apply dark Fusion theme."""
    app.setStyle("Fusion")
    p = QPalette()
    p.setColor(QPalette.ColorRole.Window, QColor(30, 30, 30))
    p.setColor(QPalette.ColorRole.WindowText, QColor(220, 220, 220))
    p.setColor(QPalette.ColorRole.Base, QColor(25, 25, 25))
    p.setColor(QPalette.ColorRole.AlternateBase, QColor(35, 35, 35))
    p.setColor(QPalette.ColorRole.Text, QColor(220, 220, 220))
    p.setColor(QPalette.ColorRole.Button, QColor(45, 45, 45))
    p.setColor(QPalette.ColorRole.ButtonText, QColor(220, 220, 220))
    p.setColor(QPalette.ColorRole.Highlight, QColor(42, 130, 218))
    p.setColor(QPalette.ColorRole.HighlightedText, QColor(255, 255, 255))
    p.setColor(QPalette.ColorRole.ToolTipBase, QColor(40, 40, 40))
    p.setColor(QPalette.ColorRole.ToolTipText, QColor(220, 220, 220))
    p.setColor(QPalette.ColorRole.PlaceholderText, QColor(120, 120, 120))
    app.setPalette(p)


def main() -> None:
    debug = os.environ.get("ASSFORGE_DEBUG", "").strip().lower() in ("1", "true", "yes", "on")
    logging.basicConfig(
        level=logging.DEBUG if debug else logging.INFO,
        format="%(name)s: %(message)s",
    )
    if debug:
        logging.getLogger("ai").setLevel(logging.DEBUG)

    app = QApplication(sys.argv)
    app.setApplicationName("AssForge")
    app.setOrganizationName("AssForge")

    setup_dark_theme(app)

    # Import here to avoid circular imports
    from app.ui.main_window import MainWindow

    window = MainWindow()
    window.show()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
