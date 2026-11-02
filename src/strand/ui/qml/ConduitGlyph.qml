import QtQuick
import QtQuick.Shapes

// 3-beat sequential conduit glyph (grounding strike -> forward egress -> reverse ingress).
// Geometry lives in a 120x120 design space and is scaled to the item size. Idle is pure
// static rendering: the single phase animation is `running: false` unless busy/distilling
// (OBS-ERG-003), and the whole glyph is its own layer so opacity changes never invalidate
// the window canvas.
Item {
    id: glyph
    width: 24
    height: 24

    readonly property color cyan: "#38bdf8"
    readonly property color amber: "#f59e0b"
    readonly property color slate: "#475569"

    // Beat inputs (null-safe when no bridge is injected).
    readonly property bool striking: typeof bridge !== "undefined" && bridge ? bridge.grounding : false
    readonly property bool egress: typeof bridge !== "undefined" && bridge ? bridge.busy : false
    readonly property bool ingress: !egress && typeof bridge !== "undefined" && bridge ? bridge.distilling : false
    readonly property bool active: egress || ingress
    readonly property color tint: ingress ? amber : cyan

    // 0..1 travelling position; reverse polarity runs right-to-left during ingress.
    property real phase: 0
    readonly property real head: ingress ? 1 - phase : phase

    layer.enabled: true
    layer.smooth: true

    // Intensity of a stream segment centred at `center` (0..1 along the conduit).
    function glow(center) {
        if (!active)
            return 0
        var d = Math.abs(head - center)
        d = Math.min(d, 1 - d)
        return Math.max(0, 1 - d / 0.28)
    }

    NumberAnimation on phase {
        id: sweep
        from: 0
        to: 1
        duration: glyph.ingress ? 1600 : 1000
        loops: Animation.Infinite
        running: glyph.active
        onRunningChanged: if (!running) glyph.phase = 0
    }

    Item {
        id: canvas
        width: 120
        height: 120
        scale: glyph.width / 120
        transformOrigin: Item.TopLeft

        // Asymmetrical slate rails.
        Rectangle { x: 14; y: 32 - 2; width: 70; height: 4; radius: 2; color: glyph.slate; opacity: 0.7 }
        Rectangle { x: 6; y: 88 - 2; width: 108; height: 4; radius: 2; color: glyph.slate; opacity: 0.7 }

        // Stream line: one smooth harmonic swell (flat tangents at 28, the 64,48 apex and 100).
        // Dim base stroke plus a travelling lit dash on the same path; the dash period is ~one
        // path length (24 stroke widths) so the wave loops seamlessly without jitter.
        Shape {
            id: stream
            objectName: "conduitStream"
            width: 120
            height: 120
            preferredRendererType: Shape.CurveRenderer

            ShapePath {
                strokeColor: Qt.alpha(glyph.tint, glyph.active ? 0.22 : 0.85)
                strokeWidth: 4
                fillColor: "transparent"
                capStyle: ShapePath.RoundCap
                joinStyle: ShapePath.RoundJoin
                startX: 20; startY: 64
                PathLine { x: 28; y: 64 }
                PathCubic { x: 64; y: 48; control1X: 44; control1Y: 64; control2X: 50; control2Y: 48 }
                PathCubic { x: 100; y: 64; control1X: 78; control1Y: 48; control2X: 84; control2Y: 64 }
                PathLine { x: 108; y: 64 }
            }

            ShapePath {
                objectName: "conduitWave"
                strokeColor: glyph.active ? glyph.tint : "transparent"
                strokeWidth: 4
                fillColor: "transparent"
                capStyle: ShapePath.RoundCap
                joinStyle: ShapePath.RoundJoin
                strokeStyle: ShapePath.DashLine
                dashPattern: [7, 17]
                dashOffset: -glyph.head * 24
                startX: 20; startY: 64
                PathLine { x: 28; y: 64 }
                PathCubic { x: 64; y: 48; control1X: 44; control1Y: 64; control2X: 50; control2Y: 48 }
                PathCubic { x: 100; y: 64; control1X: 78; control1Y: 48; control2X: 84; control2Y: 64 }
                PathLine { x: 108; y: 64 }
            }
        }

        // Central core node (apex 64,48): lit cyan for the 150 ms grounding strike.
        Rectangle {
            id: core
            objectName: "conduitCore"
            x: 64 - width / 2
            y: 48 - height / 2
            width: glyph.striking ? 26 : 18
            height: width
            radius: width / 2
            color: glyph.striking ? glyph.cyan : (glyph.active ? glyph.tint : glyph.cyan)
            opacity: glyph.striking ? 1.0 : (glyph.active ? 0.6 + 0.4 * glyph.glow(0.5) : 0.85)
            border.width: glyph.striking ? 3 : 0
            border.color: "#bae6fd"
        }
    }
}
