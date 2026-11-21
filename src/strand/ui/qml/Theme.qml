pragma Singleton
import QtQuick

// Single source of truth for the Strand UI design tokens (colours, overlays, borders, radii,
// spacing, typography). Registered through ./qmldir as a singleton, so `Theme.<property>` is
// available in every QML file. Qt.rgba channels are normalised to 0..1.
QtObject {
    // Live selector (driven by the Calibration modal / config.json): the unified theme, which
    // also fixes the operator's font. Strand's own voice is always IBM Plex Sans.
    property string uiTheme: "obsidian"          // obsidian | graphite | daylight

    // Bundled SIL OFL fonts (fonts/LICENSES.txt). `name` is empty until loaded, in which case
    // firstFamily() falls through to an installed or generic family.
    property FontLoader interLoader: FontLoader { source: Qt.resolvedUrl("fonts/Inter.ttf") }
    property FontLoader plexLoader: FontLoader { source: Qt.resolvedUrl("fonts/IBMPlexSans.ttf") }
    property FontLoader monoLoader: FontLoader { source: Qt.resolvedUrl("fonts/JetBrainsMono.ttf") }

    // Unified themes. `ink` is the wash colour: white over dark surfaces, slate over light ones.
    // `userFont` couples the operator's UI/user typeface to the theme.
    readonly property var themes: ({
        "obsidian": {
            light: false, userFont: "inter",
            canvas: "#090b0e", userBubble: "#141c2b", userBorder: Qt.rgba(99 / 255, 102 / 255, 241 / 255, 0.20),
            assistantBubble: "#11161d", artifact: "#0c0f14", artifactHeader: "#10141a",
            modal: "#10141a", drawer: "#0d1117", card: "#0c0f14", field: "#090b0e",
            text: "#f1f5f9", textPrimary: "#E6E9ED", muted: "#94a3b8", dim: "#7F8A96", faint: "#8b949e",
            ink: Qt.rgba(1, 1, 1, 1), border: "#2C323A", panel: "#1E2228", code: "#e2e8f0",
            backdrop: Qt.rgba(0, 0, 0, 0.55), onPrimary: "#0b0e12"
        },
        "graphite": {
            light: false, userFont: "jetbrains",
            canvas: "#111317", userBubble: "#1c212b", userBorder: Qt.rgba(148 / 255, 163 / 255, 184 / 255, 0.22),
            assistantBubble: "#161a20", artifact: "#13161b", artifactHeader: "#191d24",
            modal: "#15181e", drawer: "#15181e", card: "#13161b", field: "#111317",
            text: "#f1f5f9", textPrimary: "#E6E9ED", muted: "#94a3b8", dim: "#7F8A96", faint: "#8b949e",
            ink: Qt.rgba(1, 1, 1, 1), border: "#2C323A", panel: "#1E2228", code: "#e2e8f0",
            backdrop: Qt.rgba(0, 0, 0, 0.55), onPrimary: "#0b0e12"
        },
        "daylight": {
            light: true, userFont: "inter",
            canvas: "#f4f6f8", userBubble: "#e2e8f0", userBorder: Qt.rgba(15 / 255, 23 / 255, 42 / 255, 0.12),
            assistantBubble: "#ffffff", artifact: "#f8fafc", artifactHeader: "#eef2f6",
            modal: "#ffffff", drawer: "#f8fafc", card: "#f8fafc", field: "#ffffff",
            text: "#0f172a", textPrimary: "#0f172a", muted: "#475569", dim: "#475569", faint: "#64748b",
            ink: Qt.rgba(15 / 255, 23 / 255, 42 / 255, 1), border: "#cbd5e1", panel: "#ffffff", code: "#0f172a",
            backdrop: Qt.rgba(15 / 255, 23 / 255, 42 / 255, 0.35), onPrimary: "#ffffff"
        }
    })
    readonly property var themeTokens: themes[uiTheme] || themes["obsidian"]
    readonly property bool isLight: themeTokens.light
    readonly property color ink: themeTokens.ink
    // Ink wash at the given alpha (white on dark themes, slate on Daylight).
    function wash(alpha) { return Qt.rgba(ink.r, ink.g, ink.b, isLight ? Math.min(1, alpha * 1.2) : alpha) }

    // Canvas & foundation
    readonly property color canvasBackground: themeTokens.canvas
    readonly property color canvasBorder: subtleBorder
    readonly property color panel: themeTokens.panel
    readonly property color border: themeTokens.border
    readonly property color fieldBackground: themeTokens.field
    readonly property color cardBackground: themeTokens.card

    // The Voice: conversational bubbles
    readonly property color userBubbleBackground: themeTokens.userBubble
    readonly property color userBubbleBorder: themeTokens.userBorder
    readonly property color userText: themeTokens.text
    readonly property color assistantBubbleBackground: themeTokens.assistantBubble
    readonly property color assistantBubbleBorder: Qt.rgba(brandAccent.r, brandAccent.g, brandAccent.b, isLight ? 0.40 : 0.30)
    readonly property color assistantText: themeTokens.text
    readonly property color textMuted: themeTokens.muted

    // Text
    readonly property color textPrimary: themeTokens.textPrimary
    readonly property color textDim: themeTokens.dim
    readonly property color textFaint: themeTokens.faint
    readonly property color stanceText: isLight ? Qt.rgba(15 / 255, 23 / 255, 42 / 255, 0.85) : Qt.rgba(0.82, 0.85, 0.9, 0.85)
    readonly property color subtleText: wash(0.4)
    readonly property color placeholderText: wash(isLight ? 0.45 : 0.28)
    readonly property color watermarkText: wash(isLight ? 0.30 : 0.22)
    readonly property color onPrimaryText: themeTokens.onPrimary

    // The Workbench & artifacts: code, diffs, tables
    readonly property color artifactBackground: themeTokens.artifact
    readonly property color artifactHeaderBackground: themeTokens.artifactHeader
    readonly property color artifactBorder: wash(0.08)
    readonly property color codeText: themeTokens.code

    // Telemetry & accents
    readonly property color brandAccent: isLight ? "#0284c7" : "#38bdf8"         // cyan
    readonly property color brandAccentSoft: isLight ? "#0369a1" : "#7dd3fc"
    readonly property color distillationAccent: isLight ? "#b45309" : "#f59e0b"  // amber
    readonly property color warnAccent: isLight ? "#b45309" : "#fbbf24"
    readonly property color successAccent: isLight ? "#059669" : "#34d399"       // emerald
    readonly property color dangerAccent: isLight ? "#dc2626" : "#f87171"        // coral
    readonly property color actionAccent: isLight ? "#0284c7" : "#3DAEE9"        // primary buttons

    // Input dock
    readonly property color dockSurface: assistantBubbleBackground
    readonly property color dockBorder: artifactBorder
    readonly property color dockDropBorder: Qt.rgba(brandAccent.r, brandAccent.g, brandAccent.b, 0.3)

    // Overlays & elevated surfaces
    readonly property color overlayBackdrop: themeTokens.backdrop
    readonly property color drawerSurface: Qt.rgba(Qt.color(themeTokens.drawer).r, Qt.color(themeTokens.drawer).g, Qt.color(themeTokens.drawer).b, 0.97)
    readonly property color modalSurface: themeTokens.modal
    readonly property color modalBorder: isLight ? Qt.rgba(15 / 255, 23 / 255, 42 / 255, 0.12) : Qt.rgba(1, 1, 1, 0.10)

    // Drag-and-drop attach overlay
    readonly property color dropOverlayFill: Qt.rgba(brandAccent.r, brandAccent.g, brandAccent.b, 0.05)
    readonly property color dropOverlayBorder: Qt.rgba(brandAccent.r, brandAccent.g, brandAccent.b, 0.45)
    readonly property color dropBadgeSurface: isLight ? Qt.rgba(1, 1, 1, 0.95) : Qt.rgba(0.067, 0.086, 0.114, 0.9)
    readonly property color dropBadgeBorder: Qt.rgba(brandAccent.r, brandAccent.g, brandAccent.b, 0.5)

    // Translucent control surfaces (ink washes: white-on-dark, slate-on-light)
    readonly property color surfaceRecessed: wash(0.03)
    readonly property color surfaceFaint: wash(0.04)
    readonly property color controlFill: wash(0.05)
    readonly property color chipSurface: wash(0.06)
    readonly property color controlHover: wash(0.08)
    readonly property color controlHoverStrong: wash(0.10)

    // Translucent borders (Daylight: rgba(15, 23, 42, 0.12) for the standard hairline)
    readonly property color subtleBorder: isLight ? Qt.rgba(15 / 255, 23 / 255, 42 / 255, 0.12) : Qt.rgba(1, 1, 1, 0.06)
    readonly property color controlBorder: isLight ? Qt.rgba(15 / 255, 23 / 255, 42 / 255, 0.14) : Qt.rgba(1, 1, 1, 0.08)
    readonly property color chipBorder: wash(0.1)
    readonly property color borderMedium: isLight ? Qt.rgba(15 / 255, 23 / 255, 42 / 255, 0.18) : Qt.rgba(1, 1, 1, 0.12)
    readonly property color controlBorderHover: wash(0.20)
    readonly property color checkboxBorderHover: wash(0.22)

    // Metrics, radii & sizing
    readonly property int bubbleRadius: 12
    readonly property int artifactRadius: 8
    readonly property int controlRadius: 6
    readonly property int minBubbleWidth: 56
    readonly property real maxBubbleCeilingProportion: 0.78
    readonly property int hPadding: 16
    readonly property int vPadding: 10
    readonly property int intraTurnGap: 12
    readonly property int turnGutter: 36
    readonly property int dockMaxWidth: 760
    readonly property int dockMargin: 96
    readonly property int artifactHeaderHeight: 28
    readonly property int artifactPadding: 12

    // Typography: font size / line-height constants
    readonly property int bodyPixelSize: 14
    readonly property int codePixelSize: 13
    readonly property int captionPixelSize: 11
    readonly property real narrativeLineHeight: 1.65
    readonly property real narrativeLetterSpacing: 0.15

    // Typography cascade: the workstation's system font first, then portable fallbacks.
    readonly property var uiFamilies: [Qt.application.font.family, "Inter", "-apple-system", "Segoe UI", "Noto Sans", "sans-serif"]
    // Monospace substrate: code, diffs, specs and telemetry tokens.
    readonly property var monoFamilies: ["JetBrains Mono", "Fira Code", "monospace"]
    // User voice: clean Inter / system sans.
    readonly property var userFamilies: ["Inter", Qt.application.font.family, "-apple-system", "Segoe UI", "Noto Sans", "sans-serif"]
    // Assistant voice: IBM Plex Sans on every theme, an articulate proportional sans distinct from
    // the operator's Inter / JetBrains Mono, never monospaced.
    readonly property var assistantFamilies: [plexLoader.name, "IBM Plex Sans", "Source Sans 3", "Inter", "Noto Sans", "Segoe UI", "sans-serif"]

    // QML `font.family` takes one name, so the cascade is resolved to the first installed
    // family (generic names such as sans-serif/monospace are always accepted by fontconfig).
    function firstFamily(chain) {
        var installed = Qt.fontFamilies()
        for (var i = 0; i < chain.length; i++) {
            if (chain[i].length > 0 && installed.indexOf(chain[i]) >= 0)
                return chain[i]
        }
        return chain[chain.length - 1]
    }
    // Operator font is coupled to the theme: Inter (Obsidian, Daylight) or JetBrains Mono (Graphite).
    readonly property var operatorFamilies: themeTokens.userFont === "jetbrains" ? [monoLoader.name, "JetBrains Mono", "Fira Code", "monospace"]
        : [interLoader.name].concat(userFamilies)
    readonly property string uiFontFamily: firstFamily(operatorFamilies)
    readonly property string monoFontFamily: firstFamily([monoLoader.name].concat(monoFamilies))
    readonly property string userFontFamily: firstFamily(operatorFamilies)
    readonly property string assistantFontFamily: firstFamily(assistantFamilies)
}
