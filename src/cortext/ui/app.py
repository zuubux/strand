"""Launcher for the Cortext desktop chat window (`cortext-gui` / `python -m cortext.ui`)."""

from __future__ import annotations

import sys
from pathlib import Path

QML_DIR = Path(__file__).parent / "qml"
MAIN_QML = QML_DIR / "main.qml"


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
    bridge.checkConnection()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
