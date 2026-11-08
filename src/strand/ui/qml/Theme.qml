pragma Singleton
import QtQuick

// Centralized design tokens for the Strand UI. Registered through ./qmldir as a singleton,
// so `Theme.<property>` is available in main.qml. Qt.rgba channels are normalised to 0..1.
QtObject {
    // Canvas & foundation
    readonly property color canvasBackground: "#090b0e"
    readonly property color canvasBorder: Qt.rgba(1, 1, 1, 0.06)

    // The Voice: conversational bubbles
    readonly property color userBubbleBackground: "#141c2b"
    readonly property color userBubbleBorder: Qt.rgba(99 / 255, 102 / 255, 241 / 255, 0.20)
    readonly property color userText: "#f1f5f9"
    readonly property color assistantBubbleBackground: "#11161d"
    readonly property color assistantBubbleBorder: Qt.rgba(56 / 255, 189 / 255, 248 / 255, 0.18)
    readonly property color assistantText: "#f1f5f9"
    readonly property color textMuted: "#94a3b8"

    // The Workbench & artifacts: code, diffs, tables
    readonly property color artifactBackground: "#0c0f14"
    readonly property color artifactHeaderBackground: "#10141a"
    readonly property color artifactBorder: Qt.rgba(1, 1, 1, 0.08)
    readonly property color codeText: "#e2e8f0"

    // Telemetry & accents
    readonly property color brandAccent: "#38bdf8"         // cyan
    readonly property color distillationAccent: "#f59e0b"  // amber
    readonly property color successAccent: "#34d399"       // emerald
    readonly property color dangerAccent: "#f87171"        // coral

    // Metrics, radii & sizing
    readonly property int bubbleRadius: 12
    readonly property int artifactRadius: 8
    readonly property int minBubbleWidth: 56
    readonly property real maxBubbleCeilingProportion: 0.78
    readonly property int hPadding: 16
    readonly property int vPadding: 10
    readonly property int intraTurnGap: 12
    readonly property int turnGutter: 36
    readonly property int dockMaxWidth: 760
}
