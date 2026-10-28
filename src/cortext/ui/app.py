"""Launcher for the Cortext desktop chat window (`cortext-gui` / `python -m cortext.ui`)."""

from __future__ import annotations

import sys
from pathlib import Path

QML_DIR = Path(__file__).parent / "qml"
MAIN_QML = QML_DIR / "main.qml"


SETTINGS_ORG = "cortext"
SETTINGS_APP = "cortext-gui"
GEOMETRY_KEY = "window/geometry"


def make_settings():
    """QSettings store for UI state (window geometry); native format under the user's config dir."""
    from PySide6.QtCore import QSettings

    return QSettings(SETTINGS_ORG, SETTINGS_APP)


def restore_geometry(window, settings) -> bool:
    """Apply saved `x, y, width, height` to a QQuickWindow; False if absent or invalid."""
    raw = settings.value(GEOMETRY_KEY)
    try:
        x, y, width, height = (int(v) for v in raw)
    except (TypeError, ValueError):
        return False
    if width < max(window.minimumWidth(), 1) or height < max(window.minimumHeight(), 1):
        return False
    screen = window.screen()
    if screen is not None and not screen.availableGeometry().intersects(_rect(x, y, width, height)):
        return False  # saved on a monitor that is no longer attached
    window.setGeometry(x, y, width, height)
    return True


def save_geometry(window, settings) -> None:
    settings.setValue(GEOMETRY_KEY, [window.x(), window.y(), window.width(), window.height()])
    settings.sync()


def _rect(x: int, y: int, width: int, height: int):
    from PySide6.QtCore import QRect

    return QRect(x, y, width, height)


def main(argv: list[str] | None = None) -> int:
    from PySide6.QtCore import QUrl
    from PySide6.QtGui import QGuiApplication
    from PySide6.QtQml import QQmlApplicationEngine
    from PySide6.QtQuickControls2 import QQuickStyle

    from cortext.ui.bridge import Bridge

    args = sys.argv if argv is None else argv
    QQuickStyle.setStyle("Basic")  # fully custom-styled controls; no native-style overrides
    app = QGuiApplication(args)
    app.setApplicationName("Cortext")
    app.setDesktopFileName("cortext")
    bridge = Bridge()
    engine = QQmlApplicationEngine()
    engine.rootContext().setContextProperty("bridge", bridge)
    engine.load(QUrl.fromLocalFile(str(MAIN_QML)))
    if not engine.rootObjects():
        print("cortext-gui: failed to load main.qml", file=sys.stderr)
        return 1
    window = engine.rootObjects()[0]
    settings = make_settings()
    restore_geometry(window, settings)
    app.aboutToQuit.connect(lambda: save_geometry(window, settings))
    bridge.checkConnection()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
