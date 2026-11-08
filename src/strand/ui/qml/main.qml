import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import QtQuick.Dialogs

ApplicationWindow {
    id: root
    visible: true
    width: 780
    height: 1020
    minimumWidth: 420
    minimumHeight: 360
    title: "Strand"

    // Set by app.install_tray when a system tray exists: the titlebar close hides to tray.
    property bool hideOnClose: false
    onClosing: function(close) {
        if (hideOnClose) {
            close.accepted = false
            root.hide()
        }
    }
    color: theme.canvas

    // Centralized theme tokens: every colour and font stack used by the transcript,
    // identity headers, cards and input dock is defined here and nowhere else.
    QtObject {
        id: theme
        // Surfaces
        readonly property color canvas: "#090b0e"
        readonly property color panel: "#1E2228"
        readonly property color border: "#2C323A"
        // User voice: deep slate-indigo bubble, indigo hairline, clean sans.
        readonly property color userSurface: "#141c2b"
        readonly property color userBorder: Qt.rgba(99 / 255, 102 / 255, 241 / 255, 0.20)
        // Assistant voice: dark carbon bubble, cyan hairline, high-contrast text.
        readonly property color assistantSurface: "#11161d"
        readonly property color assistantBorder: Qt.rgba(56 / 255, 189 / 255, 248 / 255, 0.18)
        readonly property color assistantText: "#f1f5f9"
        // Cards / code / tables: recessed monospace substrate.
        readonly property color cardSurface: "#0c0f14"
        readonly property color cardHeader: "#10141a"
        readonly property color cardBorder: Qt.rgba(1, 1, 1, 0.06)
        readonly property color codeText: "#D6DEE7"
        // Text & accents
        readonly property color text: "#E6E9ED"
        readonly property color dim: "#7F8A96"
        readonly property color accent: "#3DAEE9"
        readonly property color cyan: "#38bdf8"
        readonly property color cyanSoft: "#7dd3fc"
        readonly property color amber: "#f59e0b"
        readonly property color warn: "#fbbf24"
        readonly property color diffAdded: "#34d399"
        readonly property color diffRemoved: "#f87171"
        // Input dock
        readonly property color dockSurface: "#11161d"
        readonly property color dockBorder: Qt.rgba(1, 1, 1, 0.08)
        readonly property color fieldSurface: canvas
        // Geometry
        readonly property int dockMaxWidth: 760
        readonly property int dockMargin: 96
    }

    readonly property color panel: theme.panel
    readonly property color border: theme.border
    readonly property color textColor: theme.text
    readonly property color dim: theme.dim
    readonly property color accent: theme.accent
    readonly property color codeBg: theme.cardSurface
    readonly property color codeHeaderBg: theme.cardHeader
    readonly property color codeBorder: theme.cardBorder
    readonly property color userRecess: theme.userSurface
    readonly property color userBorder: theme.userBorder
    readonly property color diffAdded: "#34d399"
    readonly property color diffRemoved: theme.diffRemoved
    readonly property color assistantSurface: theme.assistantSurface
    readonly property color assistantBorder: theme.assistantBorder
    readonly property color assistantText: theme.assistantText

    // Typography cascade: the workstation's system font first, then portable fallbacks.
    readonly property var uiFamilies: [Qt.application.font.family, "Inter", "-apple-system", "Segoe UI", "Noto Sans", "sans-serif"]
    // Monospace substrate: code, diffs, specs and telemetry tokens.
    readonly property var monoFamilies: ["JetBrains Mono", "Fira Code", "monospace"]
    // User voice: clean Inter / system sans.
    readonly property var userFamilies: ["Inter", Qt.application.font.family, "-apple-system", "Segoe UI", "Noto Sans", "sans-serif"]
    // Assistant voice: an articulate proportional sans, distinct from the user's, never monospaced.
    readonly property var assistantFamilies: ["IBM Plex Sans", "Source Sans 3", "Inter", "Noto Sans", "Segoe UI", "sans-serif"]

    readonly property string operatorName: bridge && bridge.operatorName ? bridge.operatorName : "Operator"

    readonly property int bodyPixelSize: 14
    readonly property real narrativeLineHeight: 1.65
    readonly property real narrativeLetterSpacing: 0.15

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
    readonly property string uiFamily: firstFamily(uiFamilies)
    readonly property string monoFamily: firstFamily(monoFamilies)
    readonly property string assistantFamily: firstFamily(assistantFamilies)
    readonly property string userFamily: firstFamily(userFamilies)

    // Sleek compact action button shared by the settings modal.
    component SleekButton: Button {
        id: sleek
        implicitHeight: 29
        leftPadding: 14
        rightPadding: 14
        topPadding: 0
        bottomPadding: 0
        hoverEnabled: true
        font.family: root.uiFamily
        font.pixelSize: 12
        font.weight: Font.Medium
        font.letterSpacing: 0.3
        HoverHandler { cursorShape: Qt.PointingHandCursor }
        contentItem: Text {
            text: sleek.text
            font: sleek.font
            color: root.textColor
            horizontalAlignment: Text.AlignHCenter
            verticalAlignment: Text.AlignVCenter
            elide: Text.ElideRight
        }
        background: Rectangle {
            radius: 6
            color: sleek.hovered ? Qt.rgba(1, 1, 1, 0.10) : Qt.rgba(1, 1, 1, 0.05)
            border.width: 1
            border.color: sleek.pressed ? theme.cyan : (sleek.hovered ? Qt.rgba(1, 1, 1, 0.20) : Qt.rgba(1, 1, 1, 0.08))
            Behavior on color { ColorAnimation { duration: 120 } }
            Behavior on border.color { ColorAnimation { duration: 120 } }
        }
    }

    // One segment of the decay-horizon pill selector.
    component SegmentButton: Button {
        id: seg
        property bool selected: false
        Layout.fillWidth: true
        implicitHeight: 28
        topPadding: 0
        bottomPadding: 0
        hoverEnabled: true
        font.family: root.uiFamily
        font.pixelSize: 12
        HoverHandler { cursorShape: Qt.PointingHandCursor }
        contentItem: Text {
            text: seg.text
            font: seg.font
            color: seg.selected ? root.textColor : root.dim
            horizontalAlignment: Text.AlignHCenter
            verticalAlignment: Text.AlignVCenter
            elide: Text.ElideRight
        }
        background: Rectangle {
            radius: 14
            color: seg.selected ? Qt.rgba(1, 1, 1, 0.10) : "transparent"
            border.width: 1
            border.color: seg.selected ? theme.cyan : (seg.hovered ? Qt.rgba(1, 1, 1, 0.12) : "transparent")
        }
    }

    // Consequence line under a privacy checkbox. Both texts are laid out and the taller one
    // sets the height, so toggling never changes the card height or reflows its siblings.
    component ConsequenceLabel: Item {
        id: consequence
        property bool active: false
        property string checkedText: ""
        property string uncheckedText: ""
        property bool warnUnchecked: false
        Layout.fillWidth: true
        Layout.leftMargin: 24
        Layout.preferredHeight: Math.max(checkedLabel.contentHeight, uncheckedLabel.contentHeight)
        Label {
            id: checkedLabel
            width: parent.width
            text: consequence.checkedText
            visible: consequence.active
            wrapMode: Text.WordWrap
            color: root.dim
            font.family: root.uiFamily
            font.pixelSize: 11
        }
        Label {
            id: uncheckedLabel
            width: parent.width
            text: consequence.uncheckedText
            visible: !consequence.active
            wrapMode: Text.WordWrap
            color: consequence.warnUnchecked ? theme.warn : root.dim
            font.family: root.uiFamily
            font.pixelSize: 11
        }
    }

    // Compact 16px checkbox with a recessed indicator and an emerald tick.
    component CompactCheckBox: CheckBox {
        id: cbox
        spacing: 8
        padding: 0
        topPadding: 3
        bottomPadding: 3
        indicator: Rectangle {
            x: cbox.leftPadding
            y: cbox.topPadding + (cbox.availableHeight - height) / 2
            width: 16
            height: 16
            radius: 3
            color: theme.fieldSurface
            border.width: 1
            border.color: cbox.checked || cbox.hovered ? Qt.rgba(1, 1, 1, 0.22) : Qt.rgba(1, 1, 1, 0.12)
            Text {
                anchors.centerIn: parent
                visible: cbox.checked
                text: "\u2713"
                color: theme.diffAdded
                font.family: root.uiFamily
                font.pixelSize: 12
                font.bold: true
            }
        }
        contentItem: Text {
            leftPadding: cbox.indicator.width + cbox.spacing
            text: cbox.text
            font.family: root.uiFamily
            font.pixelSize: 12
            color: root.textColor
            verticalAlignment: Text.AlignVCenter
        }
    }

    font.family: uiFamily

    readonly property var diag: bridge ? bridge.diagnostics : null
    property bool drawerOpen: false
    property bool settingsOpen: false
    // First run: no engine configured and nothing said yet -> Welcome Card replaces chat chrome.
    property bool welcomeDismissed: false
    readonly property bool welcomeActive: !welcomeDismissed && bridge ? (!bridge.isConfigured && transcript.count === 0) : false
    readonly property var welcomePresets: [
        { label: "Google Gemini (AI Studio)", provider: "gemini", url: "https://generativelanguage.googleapis.com/v1beta/openai", model: "gemini-3.6-flash", custom: false },
        { label: "Local Ollama (127.0.0.1:11434)", provider: "ollama", url: "http://127.0.0.1:11434/v1", model: "qwen2.5:14b", custom: false },
        { label: "OpenRouter", provider: "openrouter", url: "https://openrouter.ai/api/v1", model: "anthropic/claude-3.7-sonnet", custom: false },
        { label: "xAI (Grok)", provider: "xai", url: "https://api.x.ai/v1", model: "grok-2", custom: false },
        { label: "OpenAI Direct", provider: "custom", url: "https://api.openai.com/v1", model: "gpt-4o-mini", custom: false },
        { label: "Custom (OpenAI-compatible)", provider: "custom", url: "", model: "", custom: true }
    ]
    property int welcomeIndex: 0
    readonly property var welcomePreset: welcomePresets[welcomeIndex]
    property string welcomeModel: welcomePresets[0].model
    property var welcomeModels: []
    property bool welcomeProbed: false
    property string welcomeError: ""
    // Setup wizard: step 1 Welcome, 2 Engine, 3 Identity, 4 Workspace roots.
    property int wizardStep: 1
    property string wizardName: bridge && bridge.operatorName ? bridge.operatorName : "Operator"
    property bool wizardTelemetry: bridge ? bridge.hostTelemetryDefault : false
    property var wizardRoots: bridge ? bridge.workspaceRoots : ["~/Projects"]
    property bool wizardBusy: false
    property string extractorMessage: ""
    property bool extractorOk: false
    property string routingMessage: ""
    // True while Up/Down is stepping through previously sent prompts.
    property bool browsingHistory: false

    ListModel { id: transcript }

    function note(message) {
        transcript.append({ role: "note", body: message, pending: false, trail: "", mutations: "" })
    }

    // Context-delta whisper: nodes/rules traversed for the latest assistant turn.
    function setTrail(nodes) {
        var last = transcript.count - 1
        if (last >= 0 && transcript.get(last).role === "assistant")
            transcript.setProperty(last, "trail", nodes.join(" \u2022 "))
    }

    function escapeHtml(s) {
        return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    }

    function isDiff(lang, text) {
        var l = lang.toLowerCase()
        return l === "diff" || l === "patch" || text.indexOf("diff --git") === 0
    }

    // File path from a unified diff header, when present.
    function diffTitle(text) {
        var m = /^\+\+\+ (?:b\/)?(\S+)/m.exec(text)
        if (m && m[1] !== "/dev/null")
            return m[1]
        m = /^--- (?:a\/)?(\S+)/m.exec(text)
        return m && m[1] !== "/dev/null" ? m[1] : ""
    }

    // Rich text for a unified diff: removed lines coral, added lines emerald.
    function diffHtml(text) {
        var out = []
        var lines = text.split("\n")
        for (var i = 0; i < lines.length; i++) {
            var line = lines[i]
            var color = theme.codeText
            if (line.indexOf("+++") === 0 || line.indexOf("---") === 0)
                color = root.dim
            else if (line.indexOf("@@") === 0)
                color = root.accent
            else if (line.charAt(0) === "+")
                color = root.diffAdded
            else if (line.charAt(0) === "-")
                color = root.diffRemoved
            out.push("<span style=\"color:" + color + "\">" + escapeHtml(line) + "</span>")
        }
        return "<pre style=\"margin:0\">" + out.join("\n") + "</pre>"
    }

    function newSession() {
        // The bridge aborts a running turn, rotates session_id and emits conversationReset.
        bridge.newConversation()
        input.text = ""
        input.forceActiveFocus()
    }

    // Fiber decay horizon: "sprint" (7d), "balanced" (30d) or "permanent" (decay off).
    property string decayHorizon: "permanent"

    function selectHorizon(name) {
        var on = name !== "permanent"
        var data = { decay_enabled: on }
        if (on)
            data.decay_half_life_days = name === "sprint" ? 7.0 : 30.0
        if (persist(data))
            decayHorizon = name
    }

    function toggleDrawer() { drawerOpen = !drawerOpen }

    // Tab while streaming: fast-forward the paced buffer onto the transcript at once.
    function flushStream() {
        if (bridge && bridge.busy)
            bridge.flush_stream()
    }

    // Esc: stop the running stream; else dismiss the telemetry drawer; else clear the input.
    function openSettings() {
        if (welcomeActive)
            return
        var c = bridge.getConfig()
        upstreamUrlEdit.text = c.upstream_url
        modelEdit.text = c.model_default
        apiKeyEdit.text = ""
        apiKeyEdit.placeholderText = c.upstream_api_key_set ? "\u2022\u2022\u2022\u2022\u2022\u2022\u2022\u2022\u2022\u2022\u2022\u2022\u2022\u2022\u2022\u2022  (anchored to disk 0600)" : "Enter upstream API key..."
        extractorUrlEdit.text = c.extractor_url
        extractorModelBox.editText = c.extractor_model
        redactBox.checked = c.redact_secrets
        decayHorizon = !c.decay_enabled ? "permanent" : (c.decay_half_life_days <= 7.5 ? "sprint" : "balanced")
        extractorMessage = ""
        bridge.refreshActiveStance()
        routingMessage = ""
        settingsOpen = true
        bridge.probeExtractorModels(root.extractorProbeUrl())
    }

    // Extractor endpoint to probe: the typed URL, else the placeholder default (local Ollama).
    function extractorProbeUrl() {
        var url = (extractorUrlEdit.text && extractorUrlEdit.text.trim().length > 0) ? extractorUrlEdit.text.trim() : "http://127.0.0.1:11434/v1"
        return url
    }

    function welcomeEndpoint() {
        return welcomePreset.custom ? welcomeEndpointEdit.text.trim() : welcomePreset.url
    }

    // Provider change: reset discovered models, apply the preset default; Ollama probes immediately.
    function selectWelcomeProvider(index) {
        welcomeProbeTimer.stop()
        welcomeIndex = index
        welcomeProbed = false
        welcomeModels = []
        welcomeModel = welcomePreset.model
        welcomeModelBox.editText = welcomeModel
        welcomeError = ""
        if (welcomePreset.provider === "ollama") {
            welcomeProbed = true
            bridge.probeModels("http://127.0.0.1:11434/v1", "", "ollama")
        }
    }

    // Debounced (400 ms) probe: a pasted key of >= 20 chars (and a URL for Custom) triggers discovery.
    function scheduleWelcomeProbe() {
        welcomeProbeTimer.restart()
    }

    function probeWelcomeModels() {
        var key = welcomeKeyEdit.text.trim()
        var url = welcomeEndpoint()
        if (welcomePreset.provider === "ollama" || key.length < 20 || url.length === 0)
            return
        welcomeProbed = true
        bridge.probeModels(url, key, welcomePreset.provider)
    }

    function applyWelcomeModels() {
        if (!welcomeProbed)
            return  // stale result from a previous provider
        var list = bridge.availableModels
        welcomeModels = list
        if (list.length > 0 && list.indexOf(welcomeModel) < 0) {
            if (welcomePreset.provider === "ollama") {
                welcomeModel = list[0]
            } else if (welcomeModel === welcomePreset.model) {
                // Untouched preset default missing upstream: take the first lightweight (lite/flash) match.
                var light = bridge.pickDefaultModel()
                if (light.length > 0)
                    welcomeModel = light
            }
        }
        var i = list.indexOf(welcomeModel)
        if (i >= 0)
            welcomeModelBox.currentIndex = i
        welcomeModelBox.editText = welcomeModel
    }

    function wizardCanAdvanceEngine() {
        var local = welcomePreset.provider === "ollama"
        var model = (welcomeModelBox.editText || welcomeModel).trim()
        if (!local && welcomeKeyEdit.text.trim().length === 0) {
            welcomeError = "Enter an API key to continue."
            return false
        }
        if (welcomePreset.custom && welcomeEndpointEdit.text.trim().length === 0) {
            welcomeError = "Enter an endpoint URL to continue."
            return false
        }
        if (!local && model.length === 0) {
            welcomeError = "Enter a model name to continue."
            return false
        }
        welcomeError = ""
        return true
    }

    function wizardNext() {
        if (wizardStep === 2 && !wizardCanAdvanceEngine())
            return
        welcomeError = ""
        if (wizardStep < 4)
            wizardStep += 1
    }

    function wizardBack() {
        welcomeError = ""
        if (wizardStep > 1)
            wizardStep -= 1
    }

    function addWizardRoot(path) {
        if (path.length === 0 || wizardRoots.indexOf(path) >= 0)
            return
        wizardRoots = wizardRoots.concat([path])
    }

    function removeWizardRoot(index) {
        var copy = wizardRoots.slice()
        copy.splice(index, 1)
        wizardRoots = copy
    }

    // Finish & Anchor: one atomic save (engine, name, telemetry, roots), operator anchor, drop-in.
    function finishSetup() {
        welcomeError = ""
        var local = welcomePreset.provider === "ollama"
        var result = bridge.completeSetup({
            provider: welcomePreset.provider,
            api_key: local ? "" : welcomeKeyEdit.text,
            model: (welcomeModelBox.editText || welcomeModel).trim(),
            endpoint_url: welcomeEndpoint(),
            operator: wizardName.trim(),
            telemetry: wizardTelemetry,
            roots: wizardRoots
        })
        if (!result.ok) {
            welcomeError = result.error
            if (result.step)
                wizardStep = result.step
            return false
        }
        welcomeKeyEdit.text = ""
        welcomeDismissed = true
        transcript.append({ role: "assistant", body: result.ready, pending: false, trail: "", mutations: "" })
        Qt.callLater(function() { input.forceActiveFocus() })
        return true
    }

    function closeSettings() {
        settingsOpen = false
        input.forceActiveFocus()
    }

    // Persists `data`; each caller shows its own local feedback from the result.
    function persist(data) {
        return bridge.saveConfig(data)
    }

    function handleEscape() {
        if (settingsOpen) {
            closeSettings()
            return
        }
        if (drawerOpen && !bridge.busy)
            drawerOpen = false
        else if (bridge.busy)
            bridge.abort()
        else
            input.text = ""
        browsingHistory = false
    }

    function historyStep(older) {
        if (older && input.text.length > 0 && !browsingHistory)
            return false
        if (!older && !browsingHistory)
            return false
        var value = older ? bridge.historyPrevious() : bridge.historyNext()
        browsingHistory = older || value.length > 0
        input.text = value
        input.cursorPosition = input.length
        return true
    }

    ListModel { id: attachedFiles }

    function formatTokens(n) {
        return n >= 1000 ? "~" + Math.round(n / 1000) + "k tokens" : "~" + n + " tokens"
    }

    function attachDropped(urls) {
        var rejected = false
        for (var i = 0; i < urls.length; i++) {
            var info = bridge.inspectDroppedFile(urls[i].toString())
            if (!info.safe) {
                rejected = true
                continue
            }
            var dup = false
            for (var j = 0; j < attachedFiles.count; j++)
                if (attachedFiles.get(j).path === info.path)
                    dup = true
            if (!dup)
                attachedFiles.append({ path: info.path, name: info.name, tokens: info.tokens, isImage: info.is_image })
        }
        if (rejected) {
            dropWarning.visible = true
            dropWarningTimer.restart()
        }
    }

    function send() {
        var text = input.text.trim()
        if (text.length === 0)
            return
        browsingHistory = false
        if (text.charAt(0) === "/") {
            // Client-side commands never leave the machine.
            var feedback = bridge.runCommand(text)
            if (feedback.length > 0) {
                input.text = ""
                note(feedback)
                return
            }
        }
        if (bridge.busy)
            return
        transcript.append({ role: "user", body: text, pending: false, trail: "", mutations: "" })
        transcript.append({ role: "assistant", body: "", pending: true, trail: "", mutations: "" })
        input.text = ""
        var paths = []
        for (var i = 0; i < attachedFiles.count; i++)
            paths.push(attachedFiles.get(i).path)
        attachedFiles.clear()
        bridge.sendMessage(text, paths)
    }

    function finishPending(replacement) {
        var last = transcript.count - 1
        if (last >= 0 && transcript.get(last).pending) {
            if (replacement.length > 0)
                transcript.setProperty(last, "body", replacement)
            transcript.setProperty(last, "pending", false)
        }
    }

    Connections {
        target: bridge
        function onChunkReceived(token) {
            var last = transcript.count - 1
            if (last >= 0 && transcript.get(last).pending)
                transcript.setProperty(last, "body", transcript.get(last).body + token)
        }
        function onStreamFinished() { root.finishPending("") }
        function onErrorOccurred(message) {
            var last = transcript.count - 1
            var partial = last >= 0 ? transcript.get(last).body : ""
            root.finishPending((partial.length > 0 ? partial + "\n\n" : "") + "Error: " + message)
        }
        function onStreamAborted() {
            var last = transcript.count - 1
            var partial = last >= 0 ? transcript.get(last).body : ""
            root.finishPending(partial.length > 0 ? partial + "\n\n_[stopped]_" : "_[stopped]_")
        }
        function onMutationsRecorded(payload) {
            var last = transcript.count - 1
            if (last >= 0 && transcript.get(last).role === "assistant")
                transcript.setProperty(last, "mutations", payload)
        }
        function onConversationReset() { transcript.clear() }
        function onBusyChanged() { if (bridge && !bridge.busy) input.forceActiveFocus() }
    }

    // Strand narrative: unboxed editorial flow. `Text` (not TextEdit) because only it
    // supports proportional line height; Qt Quick TextEdit has no lineHeight property.
    Component {
        id: textBlock
        Text {
            wrapMode: Text.Wrap
            textFormat: Text.MarkdownText
            text: segment.text
            color: root.textColor
            linkColor: root.accent
            font.family: root.uiFamily
            font.pixelSize: root.bodyPixelSize
            font.kerning: true
            font.letterSpacing: root.narrativeLetterSpacing
            lineHeightMode: Text.ProportionalHeight
            lineHeight: root.narrativeLineHeight
        }
    }

    // Assistant dialogue bubble: hugs its content (an invisible no-wrap twin measures the
    // natural width) up to the flow width; the visible text wraps inside it, one-way.
    Component {
        id: proseBubble
        Item {
            id: pb
            readonly property real hPad: 16
            readonly property real vPad: 12
            readonly property real minBubbleWidth: 56
            readonly property real maxBubbleWidth: parent ? parent.width : 0
            // Natural unwrapped text width (+ padding); `prose` wraps to pb.width, so only the
            // invisible no-wrap `measure` twin feeds the width (one-way, no binding loop).
            readonly property real natural: Math.ceil(measure.implicitWidth) + 2 * hPad + 2
            width: Math.min(maxBubbleWidth, Math.max(minBubbleWidth, natural))
            implicitWidth: width
            implicitHeight: prose.implicitHeight + 2 * vPad
            height: implicitHeight
            Text {
                id: measure
                visible: false
                wrapMode: Text.NoWrap
                textFormat: Text.MarkdownText
                text: segment.text
                font.family: root.assistantFamily
                font.pixelSize: root.bodyPixelSize
                font.letterSpacing: root.narrativeLetterSpacing
            }
            Rectangle {
                objectName: "assistantBubble"
                anchors.fill: parent
                radius: 12
                color: root.assistantSurface
                border.color: root.assistantBorder
                border.width: 1
            }
            Text {
                id: prose
                x: pb.hPad
                y: pb.vPad
                width: pb.width - 2 * pb.hPad
                wrapMode: Text.Wrap
                textFormat: Text.MarkdownText
                text: segment.text
                color: theme.assistantText
                linkColor: root.accent
                font.family: root.assistantFamily
                font.pixelSize: root.bodyPixelSize
                font.kerning: true
                font.letterSpacing: root.narrativeLetterSpacing
                lineHeightMode: Text.ProportionalHeight
                lineHeight: root.narrativeLineHeight
            }
        }
    }

    // Pillowed technical card: recessed body, 1px hairline border, header bar with the
    // title (diff file path) and language tag. Unified diffs colour removed/added lines.
    Component {
        id: codeBlock
        Rectangle {
            id: card
            readonly property bool diff: root.isDiff(segment.lang, segment.text)
            readonly property string title: diff ? root.diffTitle(segment.text) : ""
            readonly property string tag: segment.lang.length > 0 ? segment.lang : (diff ? "diff" : "")
            implicitHeight: header.height + codeText.implicitHeight + 20
            implicitWidth: codeText.implicitWidth + 24
            radius: 10
            color: root.codeBg
            border.color: root.codeBorder
            border.width: 1
            clip: true

            Rectangle {
                id: header
                objectName: "codeHeader"
                width: parent.width
                height: 26
                color: root.codeHeaderBg
                Label {
                    anchors.left: parent.left
                    anchors.leftMargin: 12
                    anchors.right: tagLabel.left
                    anchors.rightMargin: 8
                    anchors.verticalCenter: parent.verticalCenter
                    text: card.title.length > 0 ? card.title : (card.tag.length > 0 ? "" : "snippet")
                    elide: Text.ElideMiddle
                    color: root.dim
                    font.family: root.monoFamily
                    font.pixelSize: 11
                }
                Label {
                    id: tagLabel
                    anchors.right: parent.right
                    anchors.rightMargin: 12
                    anchors.verticalCenter: parent.verticalCenter
                    visible: card.tag.length > 0
                    text: card.tag
                    color: root.accent
                    font.family: root.monoFamily
                    font.pixelSize: 10
                }
                Rectangle { anchors.bottom: parent.bottom; width: parent.width; height: 1; color: root.codeBorder }
            }
            TextEdit {
                id: codeText
                x: 12
                y: header.height + 10
                width: parent.width - 24
                readOnly: true
                selectByMouse: true
                wrapMode: TextEdit.WrapAnywhere
                textFormat: card.diff ? TextEdit.RichText : TextEdit.PlainText
                text: card.diff ? root.diffHtml(segment.text) : segment.text
                color: theme.codeText
                selectionColor: root.accent
                font.family: root.monoFamily
                font.pixelSize: 13
            }
        }
    }

    Shortcut { sequence: "Ctrl+L"; context: Qt.WindowShortcut; onActivated: root.newSession() }
    Shortcut { sequence: "Ctrl+D"; context: Qt.WindowShortcut; onActivated: root.toggleDrawer() }
    Shortcut { sequence: "Tab"; enabled: bridge ? bridge.busy : false; context: Qt.WindowShortcut; onActivated: root.flushStream() }
    Shortcut { sequence: "Escape"; context: Qt.WindowShortcut; onActivated: root.handleEscape() }

    RowLayout {
        anchors.fill: parent
        anchors.margins: 1
        spacing: 0

    ColumnLayout {
        Layout.fillWidth: true
        Layout.fillHeight: true
        spacing: 0

        Rectangle {
            Layout.fillWidth: true
            Layout.preferredHeight: 40
            color: "transparent"

            Row {
                anchors.left: parent.left
                anchors.leftMargin: 16
                anchors.verticalCenter: parent.verticalCenter
                spacing: 9
                ConduitGlyph {
                    id: brandDot
                    objectName: "brandDot"
                    width: 24
                    height: 24
                    anchors.verticalCenter: parent.verticalCenter
                }
                Label {
                    anchors.verticalCenter: parent.verticalCenter
                    text: "STRAND"
                    color: root.dim
                    font.pixelSize: 11
                    font.weight: Font.DemiBold
                    font.letterSpacing: 2.4
                }
            }

            Item {
                id: lockButton
                objectName: "lockButton"
                readonly property bool active: bridge ? bridge.privateMode : false
                anchors.right: parent.right
                anchors.rightMargin: 14
                anchors.verticalCenter: parent.verticalCenter
                width: 26
                height: 26
                Label {
                    id: lockGlyph
                    anchors.centerIn: parent
                    text: lockButton.active ? "\uD83D\uDD13\uFE0E" : "\uD83D\uDD12\uFE0E"
                    color: lockButton.active ? theme.amber : Qt.rgba(1, 1, 1, 0.28)
                    font.pixelSize: 13
                }
                MouseArea {
                    id: lockMouse
                    anchors.fill: parent
                    hoverEnabled: true
                    cursorShape: Qt.PointingHandCursor
                    onClicked: if (bridge) bridge.runCommand(bridge.privateMode ? "/private off" : "/private on")
                }
                ToolTip.visible: lockMouse.containsMouse
                ToolTip.text: lockButton.active
                    ? "\u26A0\uFE0F Private items unlocked (Click to keep local)"
                    : "Private items protected (Click to include in prompt)"
            }
        }

        ListView {
            id: list
            Layout.fillWidth: true
            Layout.fillHeight: true
            model: transcript
            clip: true
            spacing: 0
            topMargin: 14
            // Headroom: last bubble rests clear of the floating input dock.
            bottomMargin: dock.height + 32
            boundsBehavior: Flickable.StopAtBounds
            ScrollBar.vertical: ScrollBar { }

            // Follow the stream unless the user scrolled away from the bottom.
            property bool follow: true
            onMovementEnded: follow = atYEnd
            onContentHeightChanged: if (follow) Qt.callLater(positionViewAtEnd)
            onCountChanged: { follow = true; Qt.callLater(positionViewAtEnd) }

            Label {
                anchors.centerIn: parent
                visible: transcript.count === 0 && !root.welcomeActive && !welcomeCard.visible
                text: "Ask Strand anything"
                color: root.dim
                font.pixelSize: 15
            }

            // Asymmetric turn geometry: the user is a compact, recessed, right-aligned card;
            // Strand is a left-aligned slate card (max 80% / 760px; prose hugs its text, code spans the flow); notes are dim local asides.
            delegate: Item {
                id: row
                required property string role
                required property string body
                required property bool pending
                required property string trail
                required property string mutations
                required property int index
                readonly property bool isUser: role === "user"
                // Turn hierarchy: the first row of a speaker group carries an identity header and a
                // wide gap above it (30px); rows continuing a turn sit tight (6px; block gaps inside an assistant flow are semantic).
                readonly property string prevRole: index > 0 ? transcript.get(index - 1).role : ""
                // A finished assistant turn with no text (tool iteration limit) gets a dim notice
                // instead of an empty card or an orphan speaker header.
                readonly property bool emptyTurn: role === "assistant" && !pending && body.trim().length === 0
                readonly property string emptyNotice: "Unable to complete request within tool iteration limits."
                readonly property bool showHeader: (isUser || role === "assistant") && !emptyTurn && prevRole !== role
                readonly property real headerHeight: showHeader ? 24 : 0
                readonly property real topGap: index === 0 ? 0 : (prevRole === role ? 6 : (role === "note" || prevRole === "note" ? 12 : 30))
                readonly property real bodyTop: topGap + headerHeight
                // Assistant answers flow as bubbles + canvas payloads; the thinking pill, user
                // cards and notes keep the single bubble.
                readonly property bool flowMode: role === "assistant" && !emptyTurn && !(pending && body.length === 0)
                width: list.width
                height: bodyTop + (flowMode ? flow.height : bubble.height)

                // Identity header: operator name + avatar (right) / STRAND + brand pip (left).
                Row {
                    id: identity
                    objectName: row.isUser ? "userIdentity" : "assistantIdentity"
                    visible: row.showHeader
                    y: row.topGap
                    height: row.headerHeight
                    spacing: 7
                    layoutDirection: row.isUser ? Qt.RightToLeft : Qt.LeftToRight
                    x: row.isUser ? row.width - width - bubble.edge : bubble.edge
                    Rectangle {
                        anchors.verticalCenter: parent.verticalCenter
                        width: 16
                        height: 16
                        radius: 8
                        color: row.isUser ? root.userRecess : "transparent"
                        border.color: row.isUser ? root.userBorder : root.assistantBorder
                        border.width: 1
                        Label {
                            visible: row.isUser
                            anchors.centerIn: parent
                            text: root.operatorName.charAt(0).toUpperCase()
                            color: root.dim
                            font.pixelSize: 9
                            font.weight: Font.DemiBold
                        }
                        Rectangle {
                            visible: !row.isUser
                            anchors.centerIn: parent
                            width: 6
                            height: 6
                            radius: 3
                            color: theme.cyan
                            opacity: 0.85
                        }
                    }
                    Label {
                        anchors.verticalCenter: parent.verticalCenter
                        text: row.isUser ? root.operatorName.toUpperCase() : "STRAND"
                        color: row.isUser ? root.dim : theme.cyan
                        opacity: row.isUser ? 1.0 : 0.85
                        font.pixelSize: 11
                        font.weight: Font.DemiBold
                        font.letterSpacing: 1.6
                    }
                }

                Item {
                    id: bubble
                    objectName: "bubble"
                    visible: !row.flowMode
                    readonly property bool isNote: row.role === "note" || row.emptyTurn
                    // One-way geometry: bubble.width derives only from row.width and text metrics,
                    // never from `content` (which wraps to bubble.width), so there are no layout loops.
                    readonly property bool isThinking: row.pending && row.body.length === 0
                    readonly property bool isAssistant: row.role === "assistant"
                    readonly property bool boxed: row.isUser || (isAssistant && !row.emptyTurn)
                    readonly property real hPad: boxed ? root.bodyPixelSize : 0
                    readonly property real vPad: boxed ? Math.round(root.bodyPixelSize * 0.7) : 2
                    readonly property real edge: Math.round(root.bodyPixelSize * 8 / 7)
                    readonly property real maxWidth: row.isUser ? row.width * 0.72
                        : (isAssistant ? Math.min(row.width * 0.80, 760, row.width - 2 * edge) : row.width - 2 * edge)
                    TextMetrics {
                        id: textMetrics
                        font.pixelSize: root.bodyPixelSize
                        text: (bubble.isThinking ? "\u2026" : (row.emptyTurn ? row.emptyNotice : row.body))
                    }
                    readonly property real naturalWidth: (isThinking ? 24 : Math.ceil(textMetrics.advanceWidth)) + 2 * hPad + 2
                    x: row.isUser ? row.width - width - edge : edge
                    y: row.bodyTop
                    // Notes use the full column; user cards and the assistant thinking pill shrink to fit;
                    // assistant answers fill their bounded card width.
                    width: (row.isUser || (isAssistant && isThinking)) ? Math.min(maxWidth, naturalWidth) : Math.max(0, maxWidth)
                    height: content.implicitHeight + 2 * vPad

                    Rectangle {
                        anchors.fill: parent
                        visible: bubble.boxed
                        radius: 12
                        color: row.isUser ? root.userRecess : root.assistantSurface
                        border.color: row.isUser ? root.userBorder : root.assistantBorder
                        border.width: 1
                    }

                    Column {
                        id: content
                        objectName: "content"
                        x: bubble.hPad
                        y: bubble.vPad
                        width: bubble.width - 2 * bubble.hPad
                        spacing: 8

                        // User text and local notes are plain text.
                        TextEdit {
                            id: plain
                            visible: row.isUser || bubble.isNote
                            width: visible ? parent.width : 0
                            height: visible ? implicitHeight : 0
                            readOnly: true
                            selectByMouse: true
                            wrapMode: TextEdit.Wrap
                            textFormat: TextEdit.PlainText
                            text: row.emptyTurn ? row.emptyNotice : row.body
                            color: bubble.isNote ? root.dim : root.textColor
                            font.family: root.userFamily
                            font.italic: bubble.isNote
                            selectionColor: root.accent
                            font.pixelSize: root.bodyPixelSize
                        }

                        // Live thinking placeholder while awaiting the first token.
                        ConduitGlyph {
                            objectName: "thinkingGlyph"
                            visible: bubble.isThinking
                            width: visible ? 24 : 0
                            height: visible ? 24 : 0
                        }
                    }
                }

                // Hybrid assistant layout: prose in hugging bubbles; fences, tables and
                // structured data break out onto the recessed canvas.
                Column {
                    id: flow
                    objectName: "assistantFlow"
                    visible: row.flowMode
                    x: bubble.edge
                    y: row.bodyTop
                    width: Math.max(0, bubble.maxWidth)
                    spacing: 6

                    // Context-delta whisper: single-line `traversed A • B • Rule` pill.
                    Rectangle {
                        id: whisper
                        objectName: "whisper"
                        visible: row.role === "assistant" && row.trail.length > 0
                        height: visible ? 20 : 0
                        width: visible ? Math.min(parent.width, whisperText.implicitWidth + 20) : 0
                        radius: 10
                        color: "transparent"
                        border.color: root.codeBorder
                        border.width: 1
                        Label {
                            id: whisperText
                            anchors.left: parent.left
                            anchors.right: parent.right
                            anchors.margins: 10
                            anchors.verticalCenter: parent.verticalCenter
                            text: "traversed " + row.trail
                            elide: Text.ElideRight
                            maximumLineCount: 1
                            color: root.dim
                            opacity: 0.8
                            font.family: root.monoFamily
                            font.pixelSize: 10
                        }
                    }

                    // Ring-1 mutation pills: `[ op path ] [ Undo ]`, unboxed slate, one per file change.
                    Flow {
                        id: mutationRow
                        objectName: "mutationRow"
                        visible: row.role === "assistant" && mutationRepeater.count > 0
                        width: parent.width
                        spacing: 6
                        Repeater {
                            id: mutationRepeater
                            model: !bridge || row.role !== "assistant" || row.mutations.length === 0 ? [] : bridge.mutationRecords(row.mutations)
                            delegate: Row {
                                id: mutationPill
                                required property var modelData
                                readonly property string op: modelData.op
                                readonly property bool reverted: bridge ? bridge.revertedBackups.indexOf(modelData.backup_id) >= 0 : false
                                readonly property var glyphs: ({ created: "\uD83D\uDCC4 Created", modified: "\uD83D\uDCDD Modified", patched: "\u2702 Patched", moved: "\uD83D\uDCE6 Moved", removed: "\uD83D\uDDD1 Removed" })
                                spacing: 4
                                Rectangle {
                                    objectName: "mutationLabel"
                                    height: 22
                                    width: Math.min(mutationRow.width - undoPill.width - 4, pathLabel.implicitWidth + 20)
                                    radius: 11
                                    color: Qt.rgba(1, 1, 1, 0.04)
                                    border.color: root.codeBorder
                                    border.width: 1
                                    Label {
                                        id: pathLabel
                                        anchors.left: parent.left
                                        anchors.right: parent.right
                                        anchors.margins: 10
                                        anchors.verticalCenter: parent.verticalCenter
                                        text: mutationPill.glyphs[mutationPill.op] + ": " + mutationPill.modelData.path
                                        elide: Text.ElideMiddle
                                        maximumLineCount: 1
                                        color: root.dim
                                        font.family: root.monoFamily
                                        font.pixelSize: 10
                                    }
                                }
                                Rectangle {
                                    id: undoPill
                                    objectName: "undoPill"
                                    height: 22
                                    width: undoLabel.implicitWidth + 20
                                    radius: 11
                                    color: undoArea.containsMouse && !mutationPill.reverted ? Qt.rgba(1, 1, 1, 0.08) : Qt.rgba(1, 1, 1, 0.04)
                                    border.color: root.codeBorder
                                    border.width: 1
                                    Label {
                                        id: undoLabel
                                        anchors.centerIn: parent
                                        text: mutationPill.reverted ? "\u21BA Reverted" : "\u21B6 Undo"
                                        color: mutationPill.reverted ? root.dim : root.textColor
                                        opacity: mutationPill.reverted ? 0.6 : 1.0
                                        font.family: root.monoFamily
                                        font.pixelSize: 10
                                    }
                                    MouseArea {
                                        id: undoArea
                                        anchors.fill: parent
                                        hoverEnabled: true
                                        enabled: !mutationPill.reverted
                                        cursorShape: enabled ? Qt.PointingHandCursor : Qt.ArrowCursor
                                        onClicked: if (bridge) bridge.revertBackup(mutationPill.modelData.backup_id)
                                    }
                                }
                            }
                        }
                    }

                    // Semantic intra-turn gaps (the Column spacing of 6 is the floor): prose after
                    // prose stays tight (8), a pivot/volley block gets 18, and a hop to or from a
                    // technical canvas card gets 14.
                    Repeater {
                        id: blockRepeater
                        property var items: !bridge || !row.flowMode ? [] : bridge.blocks(row.body)
                        model: items
                        delegate: Item {
                            id: blockSlot
                            required property var modelData
                            required property int index
                            readonly property var prev: index > 0 ? blockRepeater.items[index - 1] : null
                            readonly property real desiredGap: prev === null ? 6
                                : (modelData.is_pivot ? 18
                                : (modelData.kind !== "prose" || prev.kind !== "prose" ? 14 : 8))
                            readonly property real extraGap: Math.max(0, desiredGap - flow.spacing)
                            width: flow.width
                            height: extraGap + blockLoader.height
                            Loader {
                                id: blockLoader
                                y: blockSlot.extraGap
                                width: blockSlot.width
                                sourceComponent: blockSlot.modelData.kind === "prose" ? proseBubble : codeBlock
                                property var segment: blockSlot.modelData
                            }
                        }
                    }
                }
            }
        }

        // Floating input dock: transparent footer, so the canvas flows to the window edge.
        Item {
            id: footer
            objectName: "inputFooter"
            Layout.fillWidth: true
            Layout.preferredHeight: dock.height + 8 + 36
            // Fully transparent: no shelf, divider or two-tone bar.

            Rectangle {
                id: dock
                objectName: "inputDock"
                readonly property real verticalPadding: input.topPadding + input.bottomPadding
                anchors.horizontalCenter: parent.horizontalCenter
                anchors.top: parent.top
                anchors.topMargin: 8
                // Fixed reading measure: never reacts to typed text.
                width: Math.max(0, Math.min(parent.width - theme.dockMargin, theme.dockMaxWidth))
                // Tracks the wrapped text height: one line (44) up to 180, then scrolls inside.
                height: Math.min(Math.max(input.contentHeight + verticalPadding, 44), 180)
                radius: 14
                // First-run: hidden and inert until setup completes, then fades in.
                visible: bridge ? bridge.isConfigured : false
                enabled: bridge ? (bridge.isConfigured && !bridge.busy) : false
                opacity: visible ? 1 : 0
                Behavior on opacity { NumberAnimation { duration: 220; easing.type: Easing.OutQuad } }
                color: theme.dockSurface
                border.width: 1
                border.color: dropArea.containsDrag ? Qt.rgba(0.220, 0.741, 0.973, 0.3)
                              : (input.activeFocus ? theme.cyan : theme.dockBorder)
                Behavior on height { NumberAnimation { duration: 90; easing.type: Easing.OutQuad } }

                Flickable {
                    id: inputFlick
                    anchors.fill: parent
                    anchors.margins: 1
                    contentWidth: width
                    contentHeight: input.contentHeight + dock.verticalPadding
                    clip: true
                    interactive: contentHeight > height
                    boundsBehavior: Flickable.StopAtBounds

                    TextArea.flickable: TextArea {
                        id: input
                        objectName: "messageInput"
                        placeholderText: "Message Strand..."
                        placeholderTextColor: Qt.rgba(1, 1, 1, 0.28)
                        color: root.textColor
                        wrapMode: TextArea.Wrap
                        font.pixelSize: 14
                        // Fixed alignment + padding: the placeholder inherits these, so the
                        // text baseline never moves on focus/blur/typing.
                        verticalAlignment: Text.AlignTop
                        topPadding: 12
                        bottomPadding: 12
                        leftPadding: 14
                        // Reserve room for the model watermark only while it is shown (empty input);
                        // once typing starts the full dock width is available, so no phantom wall
                        // forces a premature wrap (BUG-UI-001).
                        rightPadding: text.length > 0 ? 14 : modelWatermark.width + 20
                        enabled: bridge ? (bridge.isConfigured && !bridge.busy) : false
                        focus: false
                        activeFocusOnTab: bridge ? bridge.isConfigured : false
                        onTextChanged: if (text.length === 0) root.browsingHistory = false
                        background: null

                        Keys.onPressed: function(event) {
                            if (event.key === Qt.Key_Up && root.historyStep(true)) {
                                event.accepted = true
                            } else if (event.key === Qt.Key_Down && root.historyStep(false)) {
                                event.accepted = true
                            } else if ((event.key === Qt.Key_Return || event.key === Qt.Key_Enter)
                                    && !(event.modifiers & Qt.ShiftModifier)) {
                                event.accepted = true
                                root.send()
                            }
                        }
                    }
                    // Only exists once the text outgrows the 180px dock ceiling; never on hover
                    // over a single-line/empty input.
                    ScrollBar.vertical: ScrollBar {
                        policy: inputFlick.contentHeight > 180 ? ScrollBar.AsNeeded : ScrollBar.AlwaysOff
                        visible: inputFlick.contentHeight > 180
                    }
                    ScrollBar.horizontal: ScrollBar {
                        policy: ScrollBar.AlwaysOff
                        visible: false
                    }
                }

                DropArea {
                    id: dropArea
                    objectName: "dropArea"
                    anchors.fill: parent
                    keys: ["text/uri-list"]
                    onDropped: function(drop) {
                        if (drop.hasUrls)
                            root.attachDropped(drop.urls)
                    }
                }

                Label {
                    id: modelWatermark
                    objectName: "modelWatermark"
                    anchors.right: parent.right
                    anchors.rightMargin: 14
                    // Share the first-line text baseline with the input/placeholder (the label is
                    // only visible while the input is empty, i.e. a single unscrolled line).
                    y: inputFlick.y + input.baselineOffset - baselineOffset
                    text: bridge ? bridge.modelName : "gemini-3.6-flash"
                    color: Qt.rgba(1, 1, 1, 0.22)
                    font.family: root.firstFamily([Qt.application.font.family, "Inter", "-apple-system", "Segoe UI", "Noto Sans", "sans-serif"])
                    font.pointSize: 9.5
                    font.weight: Font.Normal
                    visible: opacity > 0
                    opacity: input.text.length === 0 ? 1 : 0
                    Behavior on opacity { NumberAnimation { duration: 120 } }
                }
            }

            // Attached-file chips and drop warning float above the dock (never resize it).
            Row {
                id: chipRow
                objectName: "attachedChips"
                anchors.left: dock.left
                anchors.bottom: dock.top
                anchors.bottomMargin: 6
                width: dock.width
                spacing: 6
                visible: attachedFiles.count > 0 || dropWarning.visible
                Repeater {
                    model: attachedFiles
                    delegate: Rectangle {
                        height: 24
                        width: chipLabel.implicitWidth + closeLabel.implicitWidth + 22
                        radius: 12
                        color: Qt.rgba(1, 1, 1, 0.06)
                        border.width: 1
                        border.color: Qt.rgba(1, 1, 1, 0.1)
                        Label {
                            id: chipLabel
                            anchors.left: parent.left
                            anchors.leftMargin: 10
                            anchors.verticalCenter: parent.verticalCenter
                            text: (model.isImage ? "\uD83D\uDDBC " : "\uD83D\uDCC4 ") + model.name + " \u2022 " + root.formatTokens(model.tokens)
                            color: model.tokens > 50000 ? "#f59e0b" : "#8b949e"
                            font.family: root.monoFamily
                            font.pixelSize: 11
                        }
                        Label {
                            id: closeLabel
                            anchors.right: parent.right
                            anchors.rightMargin: 8
                            anchors.verticalCenter: parent.verticalCenter
                            text: "\u2715"
                            color: closeMouse.containsMouse ? root.textColor : "#8b949e"
                            font.pixelSize: 11
                            MouseArea {
                                id: closeMouse
                                objectName: "chipRemove"
                                anchors.fill: parent
                                anchors.margins: -4
                                hoverEnabled: true
                                cursorShape: Qt.PointingHandCursor
                                onClicked: attachedFiles.remove(index)
                            }
                        }
                    }
                }
                Label {
                    id: dropWarning
                    objectName: "dropWarning"
                    visible: false
                    anchors.verticalCenter: undefined
                    text: "Path outside workspace roots"
                    color: "#f59e0b"
                    font.pixelSize: 11
                }
            }
            Timer {
                id: dropWarningTimer
                interval: 2000
                onTriggered: dropWarning.visible = false
            }

            // Corner controls: bottom-right, below the dock, over the seamless background.
            Row {
                anchors.right: parent.right
                anchors.rightMargin: 14
                anchors.bottom: parent.bottom
                anchors.bottomMargin: 4
                spacing: 4
                opacity: 0.6
                Rectangle {
                    id: telemetryButton
                    objectName: "telemetryButton"
                    width: 26
                    height: 26
                    radius: 13
                    color: telemetryButtonMouse.containsMouse ? Qt.rgba(1, 1, 1, 0.08) : "transparent"
                    Behavior on color { ColorAnimation { duration: 120 } }
                    Label {
                        anchors.centerIn: parent
                        text: "\u223F"
                        color: root.drawerOpen ? root.accent : root.dim
                        font.pixelSize: 14
                    }
                    MouseArea {
                        id: telemetryButtonMouse
                        anchors.fill: parent
                        hoverEnabled: true
                        cursorShape: Qt.PointingHandCursor
                        onClicked: root.toggleDrawer()
                    }
                    ToolTip.visible: telemetryButtonMouse.containsMouse
                    ToolTip.text: "Diagnostics (Ctrl+D)"
                }
                Rectangle {
                    id: settingsButton
                    objectName: "settingsButton"
                    width: 26
                    height: 26
                    radius: 13
                    color: settingsButtonMouse.containsMouse ? Qt.rgba(1, 1, 1, 0.08) : "transparent"
                    Behavior on color { ColorAnimation { duration: 120 } }
                    Label {
                        anchors.centerIn: parent
                        text: "\u2699\uFE0E"
                        color: root.dim
                        font.pixelSize: 14
                    }
                    MouseArea {
                        id: settingsButtonMouse
                        anchors.fill: parent
                        hoverEnabled: true
                        cursorShape: Qt.PointingHandCursor
                        onClicked: root.openSettings()
                    }
                    ToolTip.visible: settingsButtonMouse.containsMouse
                    ToolTip.text: "Settings"
                }
            }
        }
    }

    }

    // Click-away scrim: transparent, active only while the drawer is open, directly beneath it.
    MouseArea {
        id: drawerDismiss
        objectName: "drawerDismiss"
        anchors.fill: parent
        z: 95
        enabled: telemetryDrawer.open
        visible: telemetryDrawer.open
        onClicked: telemetryDrawer.open = false
    }

    // Floating overlay sheet: sits over the right edge and never participates in the layout above.
    Rectangle {
        id: telemetryDrawer
        objectName: "drawer"
        property alias open: root.drawerOpen
        z: 100
        width: 280
        height: Math.round(parent.height * 0.7)
        y: Math.round((parent.height - height) / 2)
        // Slide progress (0 closed, 1 open) is the only animated value, so window resizes
        // reflow x instantly; closed rests 80 px beyond the edge to hide any layout lag.
        property real progress: root.drawerOpen ? 1 : 0
        x: (parent.width + 80) + progress * ((parent.width - width - 12) - (parent.width + 80))
        opacity: progress
        visible: opacity > 0 && (root.drawerOpen || drawerSlideAnimation.running)
        clip: true
        radius: 16
        // Near-opaque #0d1117 surface (0.96) so transcript text never ghosts through.
        color: Qt.rgba(0.051, 0.067, 0.090, 0.96)
        border.color: Qt.rgba(1, 1, 1, 0.08)
        border.width: 1
        Behavior on progress {
            NumberAnimation { id: drawerSlideAnimation; duration: 140; easing.type: Easing.OutCubic }
        }

        // Swallow stray clicks on the sheet so they never reach the dismiss scrim.
        MouseArea { anchors.fill: parent; acceptedButtons: Qt.AllButtons }

        Flickable {
            anchors.fill: parent
            anchors.margins: 14
            contentWidth: width
            contentHeight: fiberColumn.height
            clip: true
            boundsBehavior: Flickable.StopAtBounds

            Column {
                id: fiberColumn
                width: parent.width
                spacing: 12

                Item {
                    width: parent.width
                    height: 24
                    Label {
                        anchors.verticalCenter: parent.verticalCenter
                        text: "TELEMETRY"
                        color: root.textColor
                        font.family: root.uiFamily
                        font.pixelSize: 12
                        font.bold: true
                        font.letterSpacing: 1.2
                    }
                    Rectangle {
                        objectName: "drawerClose"
                        anchors.right: parent.right
                        anchors.rightMargin: -6
                        anchors.verticalCenter: parent.verticalCenter
                        width: 24
                        height: 24
                        radius: 12
                        color: closeMouse.containsMouse ? Qt.rgba(1, 1, 1, 0.08) : "transparent"
                        Behavior on color { ColorAnimation { duration: 120 } }
                        Label { anchors.centerIn: parent; text: "\u2715"; color: root.dim; font.pixelSize: 12 }
                        MouseArea {
                            id: closeMouse
                            anchors.fill: parent
                            hoverEnabled: true
                            cursorShape: Qt.PointingHandCursor
                            onClicked: root.drawerOpen = false
                        }
                    }
                }

                Repeater {
                    model: !root.diag ? [] : [
                        { title: "Pulse & Velocity", rows: [
                            ["Time to first token", root.diag.ttft_ms + " ms"],
                            ["Round-trip latency", root.diag.total_ms + " ms"],
                            ["Throughput", root.diag.throughput]
                        ] },
                        { title: "Traversed Strands", rows: [
                            ["Turn token overhead", "~" + root.diag.token_count + " tokens"],
                            ["Session ledger", root.diag.ledger]
                        ] },
                        { title: "The Spool & Kernel", rows: [
                            ["Kernel transport", root.diag.transport],
                            ["Session ID", root.diag.sessionId]
                        ] }
                    ]
                    delegate: Column {
                        required property var modelData
                        width: fiberColumn.width
                        spacing: 6
                        Label {
                            text: modelData.title.toUpperCase()
                            color: root.dim
                            font.family: root.uiFamily
                            font.pixelSize: 10
                            font.letterSpacing: 1.0
                        }
                        Rectangle {
                            width: parent.width
                            height: cardColumn.height + 20
                            radius: 8
                            color: Qt.rgba(1, 1, 1, 0.03)
                            border.color: root.border
                            border.width: 1
                            Column {
                                id: cardColumn
                                x: 10
                                y: 10
                                width: parent.width - 20
                                spacing: 8
                                Repeater {
                                    model: modelData.rows
                                    delegate: Column {
                                        required property var modelData
                                        width: cardColumn.width
                                        spacing: 1
                                        Label { text: modelData[0]; color: root.dim; font.family: root.uiFamily; font.pixelSize: 10 }
                                        TextEdit {
                                            width: parent.width
                                            readOnly: true
                                            selectByMouse: true
                                            wrapMode: TextEdit.WrapAnywhere
                                            text: String(modelData[1])
                                            color: root.textColor
                                            font.family: root.monoFamily
                                            font.pixelSize: 11
                                            selectionColor: root.accent
                                        }
                                    }
                                }
                            }
                        }
                    }
                }
            }
        }
    }

    Connections {
        target: bridge
        function onExtractorStatusChanged(success, message) {
            root.extractorOk = success
            root.extractorMessage = message === "Connected" ? "Thread active" : message
        }
    }

    // First-run setup wizard: four plates (Welcome, Engine, Identity, Workspace), centered until finished.
    component WizardButton: Rectangle {
        id: wb
        property string label: ""
        property bool primary: true
        signal clicked()
        implicitHeight: 40
        implicitWidth: wbText.implicitWidth + 36
        radius: 8
        color: primary ? (wbMouse.containsMouse ? Qt.lighter(theme.accent, 1.1) : theme.accent)
                       : (wbMouse.containsMouse ? Qt.rgba(1, 1, 1, 0.08) : "transparent")
        border.color: primary ? "transparent" : root.codeBorder
        border.width: 1
        Behavior on color { ColorAnimation { duration: 120 } }
        Label {
            id: wbText
            anchors.centerIn: parent
            text: wb.label
            color: wb.primary ? "#0b0e12" : root.textColor
            font.pixelSize: 14
            font.weight: Font.DemiBold
        }
        MouseArea {
            id: wbMouse
            anchors.fill: parent
            hoverEnabled: true
            cursorShape: Qt.PointingHandCursor
            onClicked: wb.clicked()
        }
    }

    FolderDialog {
        id: wizardFolderDialog
        title: "Add a workspace folder"
        onAccepted: root.addWizardRoot(bridge.folderPath(selectedFolder.toString()))
    }

    Rectangle {
        id: welcomeCard
        objectName: "welcomeCard"
        z: 90
        anchors.centerIn: parent
        width: Math.min(parent.width - 48, 680)
        height: welcomeColumn.implicitHeight + 72
        radius: 16
        color: theme.cardSurface
        border.color: Qt.rgba(1, 1, 1, 0.08)
        border.width: 1
        opacity: root.welcomeActive ? 1 : 0
        visible: opacity > 0
        Behavior on opacity { NumberAnimation { duration: 320; easing.type: Easing.OutCubic } }

        ColumnLayout {
            id: welcomeColumn
            anchors.left: parent.left
            anchors.right: parent.right
            anchors.top: parent.top
            anchors.margins: 36
            spacing: 14

            Label {
                objectName: "wizardStepLabel"
                visible: root.wizardStep > 1
                text: "STEP " + root.wizardStep + " OF 4"
                color: root.dim
                font.pixelSize: 11
                font.bold: true
                font.letterSpacing: 1.2
            }

            // ---- Plate 1: Welcome ----
            ColumnLayout {
                id: plateWelcome
                objectName: "wizardPlate1"
                visible: root.wizardStep === 1
                Layout.fillWidth: true
                spacing: 14

                Image {
                    objectName: "wizardLogo"
                    source: bridge ? bridge.logoUrl : ""
                    sourceSize.width: 192
                    sourceSize.height: 192
                    Layout.preferredWidth: 96
                    Layout.preferredHeight: 96
                    Layout.alignment: Qt.AlignHCenter
                    fillMode: Image.PreserveAspectFit
                    smooth: true
                    mipmap: true
                }
                Label {
                    text: "Hi, I'm Strand."
                    color: root.textColor
                    font.pixelSize: 28
                    font.weight: Font.DemiBold
                    Layout.alignment: Qt.AlignHCenter
                }
                Label {
                    text: "A local, private memory companion for your workstation."
                    color: root.textColor
                    font.pixelSize: 15
                    wrapMode: Text.WordWrap
                    horizontalAlignment: Text.AlignHCenter
                    Layout.fillWidth: true
                }
                Label {
                    text: "Let's set a few things up so we can get right to work."
                    color: root.dim
                    font.pixelSize: 13
                    wrapMode: Text.WordWrap
                    horizontalAlignment: Text.AlignHCenter
                    Layout.fillWidth: true
                }
                WizardButton {
                    objectName: "wizardStart"
                    label: "Get Started \u2192"
                    Layout.alignment: Qt.AlignHCenter
                    Layout.topMargin: 10
                    Layout.preferredWidth: 220
                    onClicked: root.wizardNext()
                }
            }

            // ---- Plate 2: Inference engine ----
            ColumnLayout {
                id: plateEngine
                objectName: "wizardPlate2"
                visible: root.wizardStep === 2
                Layout.fillWidth: true
                spacing: 12

                Label {
                    text: "Choose an inference engine"
                    color: root.textColor
                    font.pixelSize: 22
                    font.weight: Font.DemiBold
                }
                Label {
                    text: "Strand sends your prompts to this model. Local Ollama keeps everything on this machine."
                    color: root.dim
                    font.pixelSize: 13
                    wrapMode: Text.WordWrap
                    Layout.fillWidth: true
                }

                ComboBox {
                    id: welcomeProviderBox
                    objectName: "welcomeProviderBox"
                    Layout.fillWidth: true
                    model: root.welcomePresets
                    textRole: "label"
                    currentIndex: 0
                    font.pixelSize: 13
                    onActivated: root.selectWelcomeProvider(currentIndex)
                }

                TextField {
                    id: welcomeEndpointEdit
                    objectName: "welcomeEndpointEdit"
                    visible: root.welcomePreset.custom === true
                    Layout.fillWidth: true
                    placeholderText: "Endpoint URL (e.g. http://127.0.0.1:8000/v1)"
                    color: root.textColor
                    selectionColor: root.accent
                    font.family: root.monoFamily
                    font.pixelSize: 12
                    leftPadding: 10
                    rightPadding: 10
                    background: Rectangle { radius: 6; color: theme.fieldSurface; border.color: root.codeBorder; border.width: 1 }
                    onTextChanged: root.scheduleWelcomeProbe()
                    onAccepted: root.wizardNext()
                }

                TextField {
                    id: welcomeKeyEdit
                    objectName: "welcomeKeyEdit"
                    visible: root.welcomePreset.provider !== "ollama"
                    Layout.fillWidth: true
                    echoMode: TextInput.Password
                    placeholderText: "API key"
                    color: root.textColor
                    selectionColor: root.accent
                    font.family: root.monoFamily
                    font.pixelSize: 12
                    leftPadding: 10
                    rightPadding: 10
                    background: Rectangle { radius: 6; color: theme.fieldSurface; border.color: root.codeBorder; border.width: 1 }
                    onTextChanged: root.scheduleWelcomeProbe()
                    onAccepted: root.wizardNext()
                }

                ComboBox {
                    id: welcomeModelBox
                    objectName: "welcomeModelBox"
                    Layout.fillWidth: true
                    editable: true
                    model: root.welcomeModels
                    font.family: root.monoFamily
                    font.pixelSize: 12
                    onActivated: root.welcomeModel = currentText
                    onEditTextChanged: if (activeFocus) root.welcomeModel = editText
                    Component.onCompleted: editText = root.welcomeModel
                }

                Label {
                    objectName: "welcomeProbeStatus"
                    visible: text.length > 0
                    text: !root.welcomeProbed ? ""
                        : bridge.isProbingModels ? "Probing available models..."
                        : bridge.probeStatus === "success" ? "\u2713 Found " + bridge.availableModels.length + " models"
                        : bridge.probeStatus === "failed" ? "Could not auto-detect models (enter manually)"
                        : ""
                    color: root.dim
                    font.pixelSize: 11
                    Layout.fillWidth: true
                }

                Timer {
                    id: welcomeProbeTimer
                    interval: 400
                    repeat: false
                    onTriggered: root.probeWelcomeModels()
                }
                Connections {
                    target: bridge
                    function onAvailableModelsChanged() { root.applyWelcomeModels() }
                }
            }

            // ---- Plate 3: Operator identity & telemetry ----
            ColumnLayout {
                id: plateIdentity
                objectName: "wizardPlate3"
                visible: root.wizardStep === 3
                Layout.fillWidth: true
                spacing: 12

                Label {
                    text: "What should Strand call you?"
                    color: root.textColor
                    font.pixelSize: 22
                    font.weight: Font.DemiBold
                    wrapMode: Text.WordWrap
                    Layout.fillWidth: true
                }
                TextField {
                    id: wizardNameEdit
                    objectName: "wizardNameEdit"
                    Layout.fillWidth: true
                    placeholderText: "Your name"
                    color: root.textColor
                    selectionColor: root.accent
                    font.pixelSize: 14
                    leftPadding: 10
                    rightPadding: 10
                    maximumLength: 64
                    background: Rectangle { radius: 6; color: theme.fieldSurface; border.color: root.codeBorder; border.width: 1 }
                    Component.onCompleted: text = root.wizardName
                    onTextChanged: root.wizardName = text
                    onAccepted: root.wizardNext()
                }
                CheckBox {
                    id: wizardTelemetryBox
                    objectName: "wizardTelemetryBox"
                    text: "Share local hardware specs with model"
                    checked: root.wizardTelemetry
                    onToggled: root.wizardTelemetry = checked
                    contentItem: Label {
                        text: wizardTelemetryBox.text
                        color: root.textColor
                        font.pixelSize: 14
                        leftPadding: wizardTelemetryBox.indicator.width + 8
                        verticalAlignment: Text.AlignVCenter
                    }
                }
                Label {
                    text: "Off by default. When off, the model only sees your hostname, not your OS, CPU or RAM."
                    color: root.dim
                    font.pixelSize: 12
                    wrapMode: Text.WordWrap
                    Layout.fillWidth: true
                }
            }

            // ---- Plate 4: Approved workspace roots ----
            ColumnLayout {
                id: plateRoots
                objectName: "wizardPlate4"
                visible: root.wizardStep === 4
                Layout.fillWidth: true
                spacing: 10

                Label {
                    text: "Where may Strand read?"
                    color: root.textColor
                    font.pixelSize: 22
                    font.weight: Font.DemiBold
                }
                Label {
                    text: "Strand only looks at files inside these folders, and only when you ask."
                    color: root.dim
                    font.pixelSize: 13
                    wrapMode: Text.WordWrap
                    Layout.fillWidth: true
                }
                Repeater {
                    model: root.wizardRoots
                    delegate: Rectangle {
                        required property int index
                        required property string modelData
                        Layout.fillWidth: true
                        Layout.preferredHeight: 38
                        radius: 6
                        color: theme.fieldSurface
                        border.color: root.codeBorder
                        border.width: 1
                        RowLayout {
                            anchors.fill: parent
                            anchors.leftMargin: 10
                            anchors.rightMargin: 6
                            Label {
                                Layout.fillWidth: true
                                text: modelData
                                elide: Text.ElideMiddle
                                color: root.textColor
                                font.family: root.monoFamily
                                font.pixelSize: 12
                            }
                            Rectangle {
                                objectName: "wizardRemoveRoot"
                                Layout.preferredWidth: 26
                                Layout.preferredHeight: 26
                                radius: 13
                                color: removeMouse.containsMouse ? Qt.rgba(1, 1, 1, 0.08) : "transparent"
                                Label { anchors.centerIn: parent; text: "\u2715"; color: root.dim; font.pixelSize: 12 }
                                MouseArea {
                                    id: removeMouse
                                    anchors.fill: parent
                                    hoverEnabled: true
                                    cursorShape: Qt.PointingHandCursor
                                    onClicked: root.removeWizardRoot(index)
                                }
                            }
                        }
                    }
                }
                WizardButton {
                    objectName: "wizardAddFolder"
                    label: "[+ Add Folder]"
                    primary: false
                    onClicked: wizardFolderDialog.open()
                }
            }

            Label {
                objectName: "welcomeError"
                visible: root.welcomeError.length > 0
                text: root.welcomeError
                color: theme.warn
                font.pixelSize: 12
                wrapMode: Text.WordWrap
                Layout.fillWidth: true
            }

            // Navigation for plates 2-4.
            RowLayout {
                objectName: "wizardNav"
                visible: root.wizardStep > 1
                Layout.fillWidth: true
                Layout.topMargin: 6
                spacing: 10
                WizardButton {
                    objectName: "wizardBack"
                    label: "\u2190 Back"
                    primary: false
                    onClicked: root.wizardBack()
                }
                Item { Layout.fillWidth: true }
                WizardButton {
                    objectName: "wizardNext"
                    visible: root.wizardStep < 4
                    label: "Next \u2192"
                    onClicked: root.wizardNext()
                }
                WizardButton {
                    objectName: "wizardFinish"
                    visible: root.wizardStep === 4
                    label: "Finish & Anchor"
                    onClicked: root.finishSetup()
                }
            }
        }
    }

    // Settings modal: dim backdrop (click to dismiss) beneath a centered card stack.
    MouseArea {
        id: settingsBackdrop
        objectName: "settingsBackdrop"
        anchors.fill: parent
        z: 105
        enabled: root.settingsOpen
        visible: root.settingsOpen
        onClicked: root.closeSettings()
        Rectangle { anchors.fill: parent; color: Qt.rgba(0, 0, 0, 0.55) }
    }

    Rectangle {
        id: settingsModal
        objectName: "settingsModal"
        z: 110
        visible: root.settingsOpen && !root.welcomeActive
        width: Math.min(parent.width - 64, 960)
        height: Math.min(parent.height - 48, 640)
        anchors.centerIn: parent
        radius: 16
        color: Qt.rgba(root.panel.r, root.panel.g, root.panel.b, 0.97)
        border.color: Qt.rgba(1, 1, 1, 0.10)
        border.width: 1

        // Swallow clicks on the card so they never reach the dismiss backdrop.
        MouseArea { anchors.fill: parent; acceptedButtons: Qt.AllButtons }

        ColumnLayout {
            anchors.fill: parent
            anchors.margins: 18
            spacing: 12

            RowLayout {
                Layout.fillWidth: true
                Label {
                    Layout.fillWidth: true
                    text: "STRAND CALIBRATION"
                    color: root.textColor
                    font.family: root.uiFamily
                    font.pixelSize: 12
                    font.bold: true
                    font.letterSpacing: 1.2
                }
                Rectangle {
                    objectName: "settingsClose"
                    width: 24
                    height: 24
                    radius: 12
                    color: settingsCloseMouse.containsMouse ? Qt.rgba(1, 1, 1, 0.08) : "transparent"
                    Label { anchors.centerIn: parent; text: "\u2715"; color: root.dim; font.pixelSize: 12 }
                    MouseArea {
                        id: settingsCloseMouse
                        anchors.fill: parent
                        hoverEnabled: true
                        cursorShape: Qt.PointingHandCursor
                        onClicked: root.closeSettings()
                    }
                }
            }

            RowLayout {
                Layout.fillWidth: true
                Layout.fillHeight: true
                spacing: 12

                // Column 1: identity, persona & privacy
                ColumnLayout {
                    Layout.fillWidth: true
                    Layout.preferredWidth: 1
                    Layout.fillHeight: true
                    spacing: 12

                    // Panel A: read-only active stance mirror
                    Rectangle {
                        Layout.fillWidth: true
                        Layout.fillHeight: true
                        radius: 10
                        color: Qt.rgba(1, 1, 1, 0.03)
                        border.color: Qt.rgba(1, 1, 1, 0.06)
                        ColumnLayout {
                            anchors.fill: parent
                            anchors.margins: 14
                            spacing: 6
                            Label {
                                objectName: "stanceTitle"
                                text: "ACTIVE STANCE"
                                color: root.textColor
                                font.family: root.uiFamily
                                font.pixelSize: 12
                                font.bold: true
                                font.letterSpacing: 1.0
                            }
                            Label {
                                Layout.fillWidth: true
                                text: "Learned interaction style and behavioral guardrails."
                                wrapMode: Text.WordWrap
                                color: root.dim
                                opacity: 0.8
                                font.family: root.uiFamily
                                font.pixelSize: 11
                            }
                            Column {
                                id: stanceList
                                objectName: "stanceList"
                                Layout.fillWidth: true
                                Layout.fillHeight: true
                                Layout.topMargin: 6
                                spacing: 6
                                visible: (bridge ? bridge.activeStanceNodes : []).length > 0
                                Repeater {
                                    model: (bridge ? bridge.activeStanceNodes : [])
                                    delegate: Label {
                                        width: stanceList.width
                                        text: "\u2022 " + modelData
                                        wrapMode: Text.WordWrap
                                        color: Qt.rgba(0.82, 0.85, 0.9, 0.85)
                                        lineHeight: 1.25
                                        font.family: root.uiFamily
                                        font.pixelSize: 12
                                    }
                                }
                            }
                            Label {
                                id: stancePlaceholder
                                objectName: "stancePlaceholder"
                                Layout.fillWidth: true
                                Layout.fillHeight: true
                                Layout.topMargin: 6
                                visible: (bridge ? bridge.activeStanceNodes : []).length === 0
                                text: "Adapting to your workflow. Standing directives and stylistic preferences will appear here as Strand learns your habits."
                                wrapMode: Text.WordWrap
                                color: root.dim
                                font.family: root.uiFamily
                                font.pixelSize: 12
                                font.italic: true
                                lineHeight: 1.25
                            }
                            Label {
                                objectName: "stanceFooter"
                                Layout.fillWidth: true
                                horizontalAlignment: Text.AlignRight
                                text: (bridge ? bridge.activeStanceCount : 0) + " Active Directives in Memory"
                                color: root.dim
                                font.family: root.uiFamily
                                font.pixelSize: 11
                            }
                        }
                    }

                    // Panel B: memory & privacy invariants
                    Rectangle {
                        Layout.fillWidth: true
                        Layout.preferredHeight: privacyColumn.implicitHeight + 28
                        radius: 10
                        color: Qt.rgba(1, 1, 1, 0.03)
                        border.color: Qt.rgba(1, 1, 1, 0.06)
                        ColumnLayout {
                            id: privacyColumn
                            anchors.left: parent.left
                            anchors.right: parent.right
                            anchors.top: parent.top
                            anchors.margins: 14
                            spacing: 2
                            Label {
                                text: "FIBER RETENTION & BOUNDARIES"
                                color: root.textColor
                                font.family: root.uiFamily
                                font.pixelSize: 12
                                font.bold: true
                                font.letterSpacing: 1.0
                            }
                            CompactCheckBox {
                                id: redactBox
                                objectName: "redactBox"
                                text: "Thread Secret Redaction"
                                onToggled: root.persist({ redact_secrets: checked })
                            }
                            ConsequenceLabel {
                                active: redactBox.checked
                                warnUnchecked: true
                                checkedText: "Scrub API keys, passwords, and tokens before sending prompts."
                                uncheckedText: "Warning: Credentials and tokens are stored raw and may reach external models."
                            }
                            CompactCheckBox {
                                id: webSearchBox
                                objectName: "webSearchBox"
                                text: "Ground with Live Web Search"
                                checked: bridge.webSearchEnabled
                                onToggled: bridge.setWebSearchEnabled(checked)
                            }
                            Label {
                                Layout.fillWidth: true
                                Layout.leftMargin: 24
                                text: "Allow models to consult a local web search tool (DuckDuckGo) for real-time facts. Increases latency; search snippets are never saved to memory."
                                wrapMode: Text.WordWrap
                                color: root.dim
                                font.family: root.uiFamily
                                font.pixelSize: 11
                            }
                            Label {
                                Layout.topMargin: 8
                                text: "FIBER DECAY"
                                color: root.dim
                                font.family: root.uiFamily
                                font.pixelSize: 11
                                font.capitalization: Font.AllUppercase
                                font.letterSpacing: 1.0
                            }
                            RowLayout {
                                objectName: "decayHorizon"
                                Layout.fillWidth: true
                                Layout.topMargin: 2
                                spacing: 6
                                SegmentButton {
                                    objectName: "horizonSprint"
                                    text: "Sprint (7d)"
                                    selected: root.decayHorizon === "sprint"
                                    onClicked: root.selectHorizon("sprint")
                                }
                                SegmentButton {
                                    objectName: "horizonBalanced"
                                    text: "Balanced (30d)"
                                    selected: root.decayHorizon === "balanced"
                                    onClicked: root.selectHorizon("balanced")
                                }
                                SegmentButton {
                                    objectName: "horizonPermanent"
                                    text: "Permanent"
                                    selected: root.decayHorizon === "permanent"
                                    onClicked: root.selectHorizon("permanent")
                                }
                            }
                            Label {
                                objectName: "horizonWhisper"
                                Layout.fillWidth: true
                                Layout.topMargin: 8
                                Layout.bottomMargin: 4
                                Layout.minimumHeight: 30
                                wrapMode: Text.WordWrap
                                color: root.dim
                                font.family: root.uiFamily
                                font.pixelSize: 11
                                text: root.decayHorizon === "sprint" ? "Aggressive pruning: unreferenced strands fade after 7 days."
                                    : (root.decayHorizon === "balanced" ? "Standard working memory: unreferenced strands fade over 30 days."
                                    : "Time is frozen: all strands retain permanent baseline weight.")
                            }
                        }
                    }
                }

                // Column 2: upstream routing & local extractor
                ColumnLayout {
                    Layout.fillWidth: true
                    Layout.preferredWidth: 1
                    Layout.fillHeight: true
                    spacing: 12

                    // Panel A: upstream frontier routing
                    Rectangle {
                        Layout.fillWidth: true
                        Layout.fillHeight: true
                        radius: 10
                        color: Qt.rgba(1, 1, 1, 0.03)
                        border.color: Qt.rgba(1, 1, 1, 0.06)
                        ColumnLayout {
                            anchors.fill: parent
                            anchors.margins: 14
                            spacing: 4
                            Label {
                                text: "UPSTREAM ROUTING"
                                color: root.textColor
                                font.family: root.uiFamily
                                font.pixelSize: 12
                                font.bold: true
                                font.letterSpacing: 1.0
                            }
                            Label {
                                Layout.topMargin: 4
                                text: "API ENDPOINT URL"
                                color: root.dim
                                font.family: root.uiFamily
                                font.pixelSize: 11
                                font.letterSpacing: 0.5
                            }
                            TextField {
                                id: upstreamUrlEdit
                                objectName: "upstreamUrlEdit"
                                Layout.fillWidth: true
                                placeholderText: "https://generativelanguage.googleapis.com/v1beta/openai"
                                color: root.textColor
                                selectionColor: root.accent
                                font.family: root.monoFamily
                                font.pixelSize: 12
                                leftPadding: 10
                                rightPadding: 10
                                background: Rectangle { radius: 6; color: theme.fieldSurface; border.color: root.codeBorder; border.width: 1 }
                            }
                            Label {
                                Layout.topMargin: 4
                                text: "DEFAULT MODEL"
                                color: root.dim
                                font.family: root.uiFamily
                                font.pixelSize: 11
                                font.letterSpacing: 0.5
                            }
                            TextField {
                                id: modelEdit
                                objectName: "modelEdit"
                                Layout.fillWidth: true
                                placeholderText: "gemini-3.6-flash"
                                color: root.textColor
                                selectionColor: root.accent
                                font.family: root.monoFamily
                                font.pixelSize: 12
                                leftPadding: 10
                                rightPadding: 10
                                background: Rectangle { radius: 6; color: theme.fieldSurface; border.color: root.codeBorder; border.width: 1 }
                            }
                            Label {
                                Layout.topMargin: 4
                                text: "UPSTREAM API KEY"
                                color: root.dim
                                font.family: root.uiFamily
                                font.pixelSize: 11
                                font.letterSpacing: 0.5
                            }
                            TextField {
                                id: apiKeyEdit
                                objectName: "apiKeyEdit"
                                Layout.fillWidth: true
                                echoMode: TextInput.Password
                                placeholderText: "Enter upstream API key..."
                                color: root.textColor
                                selectionColor: root.accent
                                font.family: root.monoFamily
                                font.pixelSize: 12
                                leftPadding: 10
                                rightPadding: 10
                                background: Rectangle { radius: 6; color: theme.fieldSurface; border.color: root.codeBorder; border.width: 1 }
                            }
                            Item { Layout.fillHeight: true }
                            RowLayout {
                                Layout.fillWidth: true
                                spacing: 12
                                Label {
                                    id: routingStatus
                                    objectName: "routingStatus"
                                    Layout.fillWidth: true
                                    horizontalAlignment: Text.AlignRight
                                    text: root.routingMessage
                                    color: root.routingMessage === "Route spliced" ? root.diffAdded : root.diffRemoved
                                    font.pixelSize: 11
                                }
                                SleekButton {
                                    objectName: "saveRouting"
                                    text: "Save Routing"
                                    onClicked: {
                                        root.routingMessage = root.persist({
                                            upstream_url: upstreamUrlEdit.text,
                                            model_default: modelEdit.text,
                                            upstream_api_key: apiKeyEdit.text
                                        }) ? "Route spliced" : "Save failed"
                                        apiKeyEdit.text = ""
                                    }
                                }
                            }
                        }
                    }

                    // Panel C: local fiber weaver
                    Rectangle {
                        Layout.fillWidth: true
                        Layout.preferredHeight: extractorColumn.implicitHeight + 28
                        radius: 10
                        color: Qt.rgba(1, 1, 1, 0.03)
                        border.color: Qt.rgba(1, 1, 1, 0.06)
                        ColumnLayout {
                            id: extractorColumn
                            anchors.left: parent.left
                            anchors.right: parent.right
                            anchors.top: parent.top
                            anchors.margins: 14
                            spacing: 4
                            Label {
                                text: "LOCAL FIBER WEAVER"
                                color: root.textColor
                                font.family: root.uiFamily
                                font.pixelSize: 12
                                font.bold: true
                                font.letterSpacing: 1.0
                            }
                            Label {
                                Layout.topMargin: 4
                                text: "OLLAMA / LOCAL SLM ENDPOINT"
                                color: root.dim
                                font.family: root.uiFamily
                                font.pixelSize: 11
                                font.letterSpacing: 0.5
                            }
                            Label {
                                Layout.fillWidth: true
                                text: "Local background SLM that weaves raw turns into relational strands."
                                wrapMode: Text.WordWrap
                                color: root.dim
                                opacity: 0.8
                                font.family: root.uiFamily
                                font.pixelSize: 11
                            }
                            Label {
                                Layout.topMargin: 4
                                text: "ENDPOINT URL"
                                color: root.dim
                                font.family: root.uiFamily
                                font.pixelSize: 11
                                font.letterSpacing: 0.5
                            }
                            TextField {
                                id: extractorUrlEdit
                                objectName: "extractorUrlEdit"
                                Layout.fillWidth: true
                                placeholderText: "http://127.0.0.1:11434/v1"
                                color: root.textColor
                                selectionColor: root.accent
                                font.family: root.monoFamily
                                font.pixelSize: 12
                                leftPadding: 10
                                rightPadding: 10
                                background: Rectangle { radius: 6; color: theme.fieldSurface; border.color: root.codeBorder; border.width: 1 }
                                // Saved on blur/Enter; Test Connection saves too.
                                onEditingFinished: root.persist({ extractor_url: text })
                            }
                            Label {
                                Layout.topMargin: 4
                                text: "EXTRACTOR MODEL"
                                color: root.dim
                                font.family: root.uiFamily
                                font.pixelSize: 11
                                font.letterSpacing: 0.5
                            }
                            ComboBox {
                                id: extractorModelBox
                                objectName: "extractorModelBox"
                                Layout.fillWidth: true
                                editable: true
                                model: (bridge ? bridge.availableExtractorModels : [])
                                font.family: root.monoFamily
                                font.pixelSize: 12
                                // Free typing stays possible for custom endpoints or when offline.
                                onActivated: root.persist({ extractor_model: currentText })
                                onAccepted: root.persist({ extractor_model: editText })
                                onActiveFocusChanged: if (!activeFocus && settingsOpen) root.persist({ extractor_model: editText })
                                function applyDetected() {
                                    var list = (bridge ? bridge.availableExtractorModels : [])
                                    if (list.length === 0)
                                        return
                                    var configured = bridge.getConfig().extractor_model
                                    var i = configured ? list.indexOf(configured) : -1
                                    if (i >= 0) {
                                        currentIndex = i
                                    } else if (!configured || configured.length === 0) {
                                        currentIndex = 0
                                    }
                                    if (i >= 0 || !configured || configured.length === 0)
                                        editText = list[currentIndex]
                                    else
                                        editText = configured
                                }
                                Connections {
                                    target: bridge
                                    function onAvailableExtractorModelsChanged() {
                                        extractorModelBox.applyDetected()
                                    }
                                }
                            }
                            Label {
                                objectName: "extractorModelsStatus"
                                visible: text.length > 0
                                text: (bridge ? bridge.extractorProbeStatus : "") === "success" && (bridge ? bridge.availableExtractorModels : []).length > 0
                                    ? "\u2713 Detected " + (bridge ? bridge.availableExtractorModels : []).length + " local models"
                                    : (bridge ? bridge.extractorProbeStatus : "") === "failed" || ((bridge ? bridge.extractorProbeStatus : "") === "success" && (bridge ? bridge.availableExtractorModels : []).length === 0)
                                    ? "Ollama offline (start with 'ollama serve')"
                                    : ""
                                color: (bridge ? bridge.availableExtractorModels : []).length > 0 ? root.diffAdded : root.dim
                                font.pixelSize: 11
                                Layout.fillWidth: true
                            }
                            RowLayout {
                                Layout.fillWidth: true
                                spacing: 12
                                Label {
                                    objectName: "extractorStatus"
                                    Layout.fillWidth: true
                                    horizontalAlignment: Text.AlignRight
                                    text: root.extractorMessage
                                    color: root.extractorOk ? root.diffAdded : (root.extractorMessage === "Testing\u2026" ? root.dim : root.diffRemoved)
                                    font.pixelSize: 11
                                }
                                SleekButton {
                                    objectName: "testConnection"
                                    text: "Test Connection"
                                    onClicked: {
                                        root.persist({ extractor_url: extractorUrlEdit.text, extractor_model: extractorModelBox.editText })
                                        root.extractorOk = false
                                        root.extractorMessage = "Testing\u2026"
                                        var targetUrl = (extractorUrlEdit.text && extractorUrlEdit.text.trim().length > 0) ? extractorUrlEdit.text.trim() : "http://127.0.0.1:11434/v1"
                                        bridge.testExtractor(targetUrl)
                                    }
                                }
                            }
                        }
                    }
                }
            }
        }
    }

    Rectangle {
        anchors.fill: parent
        color: "transparent"
        border.color: root.border
        border.width: 1
    }

    Component.onCompleted: input.forceActiveFocus()
}
