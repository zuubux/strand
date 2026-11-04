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
LOCAL_SCHEMES = ("file", "qrc", "data")


def fallback_icon_path() -> str:
    """First existing strand.svg: repo `packaging/icons` (source/editable) or the user hicolor dir."""
    candidates = (
        Path(__file__).resolve().parents[3] / "packaging" / "icons" / "strand.svg",
        Path.home() / ".local" / "share" / "icons" / "hicolor" / "scalable" / "apps" / "strand.svg",
    )
    for path in candidates:
        if path.is_file():
            return str(path)
    return str(candidates[0])


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


def install_egress_guard(engine) -> object:
    """Make the QML engine's network access manager refuse every non-local request (SEC-NET-007).

    Defense in depth behind `bridge.neutralize_images`: only `file`, `qrc` and `data` URLs load.
    Returns the factory; the caller must keep it alive as long as the engine.
    """
    from PySide6.QtNetwork import QNetworkAccessManager, QNetworkRequest
    from PySide6.QtQml import QQmlNetworkAccessManagerFactory

    class _LocalOnlyManager(QNetworkAccessManager):
        def createRequest(self, op, request, outgoing_data=None):  # noqa: N802 - Qt override
            if request.url().scheme().lower() not in LOCAL_SCHEMES:
                request = QNetworkRequest(request)
                request.setUrl("strand-blocked:")
            return super().createRequest(op, request, outgoing_data)

    class _Factory(QQmlNetworkAccessManagerFactory):
        def create(self, parent):
            return _LocalOnlyManager(parent)

    factory = _Factory()
    engine.setNetworkAccessManagerFactory(factory)
    return factory


def ensure_stdio() -> None:
    """Give detached launches (`Terminal=false`) usable stdout/stderr.

    Desktop launchers may start the process with closed standard streams, leaving
    `sys.stdout`/`sys.stderr` as None; any later print or Qt message would then raise or
    block. Missing streams become a devnull sink; live streams are left untouched.
    """
    import os

    for name in ("stdout", "stderr"):
        if getattr(sys, name) is None:
            setattr(sys, name, open(os.devnull, "w", encoding="utf-8"))  # noqa: SIM115


TRAY_TOOLTIP = "Strand — Working Memory Companion"


class TrayController:
    """System tray presence: toggles the window and owns the tray context menu (UI-3).

    Purely event-driven (Qt signals only): no timers or polling, so it idles at 0% CPU.
    """

    def __init__(self, app, window, bridge, tray, menu, actions) -> None:
        self.app = app
        self.window = window
        self.bridge = bridge
        self.tray = tray
        self.menu = menu
        self.actions = actions

    def is_foreground(self) -> bool:
        from PySide6.QtGui import QWindow

        w = self.window
        return bool(w.isVisible() and w.isActive() and w.visibility() != QWindow.Visibility.Minimized)

    def show_window(self) -> None:
        from PySide6.QtGui import QWindow

        if self.window.visibility() == QWindow.Visibility.Minimized:
            self.window.showNormal()
        else:
            self.window.show()
        self.window.raise_()
        self.window.requestActivate()

    def toggle_window(self) -> None:
        if self.is_foreground():
            self.window.hide()
        else:
            self.show_window()

    def on_activated(self, reason) -> None:
        from PySide6.QtWidgets import QSystemTrayIcon

        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            self.toggle_window()

    def new_session(self) -> None:
        from PySide6.QtCore import QMetaObject, QObject

        # Same path as Ctrl+L: the QML `newSession()` rotates the session and clears the input.
        if not (isinstance(self.window, QObject) and QMetaObject.invokeMethod(self.window, "newSession")):
            self.bridge.newConversation()

    def quit(self) -> None:
        self.bridge.shutdown()
        self.app.quit()


def install_tray(app, window, bridge, icon) -> TrayController | None:
    """Create the tray icon and menu; None (window-close quits) if no system tray exists."""
    from PySide6.QtWidgets import QMenu, QSystemTrayIcon

    if not QSystemTrayIcon.isSystemTrayAvailable():
        return None
    menu = QMenu()
    open_action = menu.addAction("Open Strand")
    session_action = menu.addAction("New Session")
    menu.addSeparator()
    quit_action = menu.addAction("Quit Strand UI")
    tray = QSystemTrayIcon(icon, app)
    tray.setToolTip(TRAY_TOOLTIP)
    tray.setContextMenu(menu)
    controller = TrayController(
        app, window, bridge, tray, menu, {"open": open_action, "new": session_action, "quit": quit_action}
    )
    open_action.triggered.connect(lambda: controller.show_window())
    session_action.triggered.connect(lambda: controller.new_session())
    quit_action.triggered.connect(lambda: controller.quit())
    tray.activated.connect(controller.on_activated)
    tray.show()
    window.setProperty("hideOnClose", True)  # QML onClosing vetoes the close and hides instead
    app.setQuitOnLastWindowClosed(False)  # a hidden window must not end app.exec()
    return controller


def main(argv: list[str] | None = None) -> int:
    ensure_stdio()
    from PySide6.QtCore import QTimer, QUrl
    from PySide6.QtGui import QIcon
    from PySide6.QtWidgets import QApplication
    from PySide6.QtQml import QQmlApplicationEngine
    from PySide6.QtQuickControls2 import QQuickStyle

    from strand.ui.bridge import Bridge

    args = sys.argv if argv is None else argv
    QQuickStyle.setStyle("Basic")  # fully custom-styled controls; no native-style overrides
    app = QApplication(args)  # QSystemTrayIcon requires the widgets application class
    app.setApplicationName("Strand")
    app.setDesktopFileName("strand")  # Wayland app_id -> strand.desktop and its icon
    app.setWindowIcon(QIcon.fromTheme("strand", QIcon(fallback_icon_path())))
    app.setQuitOnLastWindowClosed(True)  # closing the window ends app.exec()
    bridge = Bridge()
    engine = QQmlApplicationEngine()
    egress_guard = install_egress_guard(engine)  # noqa: F841 - kept alive with the engine
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
    tray = install_tray(app, window, bridge, QIcon.fromTheme("strand", QIcon(fallback_icon_path())))

    def on_closing() -> None:
        if tray is not None and window.property("hideOnClose"):
            return  # QML hides the window; the tray keeps the process alive
        bridge.shutdown()
        app.quit()

    window.closing.connect(on_closing)
    bridge.checkConnection()
    code = app.exec()
    bridge.shutdown()  # idempotent; guarantees no worker thread outlives exec()
    engine.deleteLater()
    return int(code)


if __name__ == "__main__":
    sys.exit(main())
