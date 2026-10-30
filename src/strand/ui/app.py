"""Launcher for the Strand desktop chat window (`strand-gui` / `python -m strand.ui`)."""

from __future__ import annotations

import sys
from pathlib import Path

QML_DIR = Path(__file__).parent / "qml"
MAIN_QML = QML_DIR / "main.qml"


SETTINGS_ORG = "strand"
SETTINGS_APP = "strand-gui"
GEOMETRY_KEY = "window/geometry"
WIDTH_KEY = "window/width"
HEIGHT_KEY = "window/height"
MIN_WIDTH = 480
MIN_HEIGHT = 500
SAVE_DELAY_MS = 300


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


def save_size(window, settings) -> None:
    """Persist width/height (the only geometry a Wayland compositor lets clients own)."""
    settings.setValue(WIDTH_KEY, int(window.width()))
    settings.setValue(HEIGHT_KEY, int(window.height()))
    settings.sync()


def restore_size(window, settings) -> bool:
    """Apply saved width/height clamped to MIN_WIDTH/MIN_HEIGHT; False if absent or invalid."""
    try:
        width = int(settings.value(WIDTH_KEY))
        height = int(settings.value(HEIGHT_KEY))
    except (TypeError, ValueError):
        return False
    window.setWidth(max(width, MIN_WIDTH))
    window.setHeight(max(height, MIN_HEIGHT))
    return True


def save_geometry(window, settings) -> None:
    save_size(window, settings)
    settings.setValue(GEOMETRY_KEY, [window.x(), window.y(), window.width(), window.height()])
    settings.sync()


def restore_window(window, settings, platform: str = "") -> None:
    """Restore size everywhere; restore (x, y) only where the platform allows it.

    Wayland compositors ignore client positioning, so position is skipped there, and any
    failure while positioning is swallowed.
    """
    if platform.lower() != "wayland":
        try:
            restore_geometry(window, settings)
        except Exception:  # noqa: BLE001 - positioning is best effort
            pass
    try:
        restore_size(window, settings)
    except Exception:  # noqa: BLE001
        pass


def _rect(x: int, y: int, width: int, height: int):
    from PySide6.QtCore import QRect

    return QRect(x, y, width, height)


def main(argv: list[str] | None = None) -> int:
    from PySide6.QtCore import QTimer, QUrl
    from PySide6.QtGui import QGuiApplication
    from PySide6.QtQml import QQmlApplicationEngine
    from PySide6.QtQuickControls2 import QQuickStyle

    from strand.ui.bridge import Bridge

    args = sys.argv if argv is None else argv
    QQuickStyle.setStyle("Basic")  # fully custom-styled controls; no native-style overrides
    app = QGuiApplication(args)
    app.setApplicationName("Strand")
    app.setDesktopFileName("strand")
    app.setQuitOnLastWindowClosed(True)  # closing the window ends app.exec()
    bridge = Bridge()
    engine = QQmlApplicationEngine()
    engine.rootContext().setContextProperty("bridge", bridge)
    engine.load(QUrl.fromLocalFile(str(MAIN_QML)))
    if not engine.rootObjects():
        print("strand-gui: failed to load main.qml", file=sys.stderr)
        return 1
    window = engine.rootObjects()[0]
    settings = make_settings()
    restore_window(window, settings, app.platformName())

    def persist() -> None:
        try:
            save_geometry(window, settings)
        except Exception:  # noqa: BLE001 - never fail on shutdown or resize
            pass

    timer = QTimer(app)
    timer.setSingleShot(True)
    timer.setInterval(SAVE_DELAY_MS)
    timer.timeout.connect(persist)
    window.widthChanged.connect(timer.start)
    window.heightChanged.connect(timer.start)
    app.aboutToQuit.connect(persist)
    app.aboutToQuit.connect(bridge.shutdown)  # cancel and join every strand-ui-io worker
    # Qt Quick's `closing` carries a QQuickCloseEvent* PySide cannot convert; a lambda that
    # accepts arguments (even `*_`) still receives it and raises TypeError, so take none.
    window.closing.connect(lambda: bridge.shutdown())
    window.closing.connect(lambda: app.quit())
    bridge.checkConnection()
    code = app.exec()
    bridge.shutdown()  # idempotent; guarantees no worker thread outlives exec()
    engine.deleteLater()
    return code


if __name__ == "__main__":
    sys.exit(main())
