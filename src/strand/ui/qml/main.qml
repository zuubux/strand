import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

ApplicationWindow {
    id: root
    visible: true
    width: 760
    height: 640
    minimumWidth: 420
    minimumHeight: 360
    title: "Strand"
    color: "#16191D"

    readonly property color panel: "#1E2228"
    readonly property color border: "#2C323A"
    readonly property color userBubble: "#2B5C8A"
    readonly property color botBubble: "#232830"
    readonly property color textColor: "#E6E9ED"
    readonly property color dim: "#7F8A96"
    readonly property color accent: "#3DAEE9"
    readonly property color codeBg: "#0B0D10"
    readonly property color codeHeaderBg: "#13161B"
    readonly property color codeBorder: Qt.rgba(1, 1, 1, 0.06)
    readonly property color userRecess: "#12161B"
    readonly property color userBorder: "#34404E"
    readonly property color diffAdded: "#34d399"
    readonly property color diffRemoved: "#f87171"

    // Typography cascade: the workstation's system font first, then portable fallbacks.
    readonly property var uiFamilies: [Qt.application.font.family, "Inter", "-apple-system", "Segoe UI", "Noto Sans", "sans-serif"]
    // Monospace substrate: code, diffs, specs and telemetry tokens.
    readonly property var monoFamilies: ["JetBrains Mono", "Fira Code", "monospace"]

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
            border.color: sleek.pressed ? "#38bdf8" : (sleek.hovered ? Qt.rgba(1, 1, 1, 0.20) : Qt.rgba(1, 1, 1, 0.08))
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
            border.color: seg.selected ? "#38bdf8" : (seg.hovered ? Qt.rgba(1, 1, 1, 0.12) : "transparent")
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
            color: consequence.warnUnchecked ? "#fbbf24" : root.dim
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
            color: "#090b0e"
            border.width: 1
            border.color: cbox.checked || cbox.hovered ? Qt.rgba(1, 1, 1, 0.22) : Qt.rgba(1, 1, 1, 0.12)
            Text {
                anchors.centerIn: parent
                visible: cbox.checked
                text: "\u2713"
                color: "#34d399"
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
    property string extractorMessage: ""
    property bool extractorOk: false
    property string stanceMessage: ""
    property string routingMessage: ""
    // True while Up/Down is stepping through previously sent prompts.
    property bool browsingHistory: false

    ListModel { id: transcript }

    function note(message) {
        transcript.append({ role: "note", body: message, pending: false, trail: "" })
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
            var color = "#D6DEE7"
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

    // Esc: stop the running stream; else dismiss the telemetry drawer; else clear the input.
    function openSettings() {
        var c = bridge.getConfig()
        personaEdit.text = c.persona_prompt
        upstreamUrlEdit.text = c.upstream_url
        modelEdit.text = c.model_default
        apiKeyEdit.text = ""
        apiKeyEdit.placeholderText = c.upstream_api_key_set ? "\u2022\u2022\u2022\u2022\u2022\u2022\u2022\u2022\u2022\u2022\u2022\u2022\u2022\u2022\u2022\u2022  (anchored to disk 0600)" : "Enter upstream API key..."
        extractorUrlEdit.text = c.extractor_url
        extractorModelEdit.text = c.extractor_model
        failClosedBox.checked = c.fail_closed_private
        redactBox.checked = c.redact_secrets
        decayHorizon = !c.decay_enabled ? "permanent" : (c.decay_half_life_days <= 7.5 ? "sprint" : "balanced")
        extractorMessage = ""
        stanceMessage = ""
        routingMessage = ""
        settingsOpen = true
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
                if (bridge.busy)  // /onboard: Strand opens, so show its streaming reply
                    transcript.append({ role: "assistant", body: "", pending: true, trail: "" })
                return
            }
        }
        if (bridge.busy)
            return
        transcript.append({ role: "user", body: text, pending: false, trail: "" })
        transcript.append({ role: "assistant", body: "", pending: true, trail: "" })
        input.text = ""
        bridge.sendMessage(text)
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
                color: "#D6DEE7"
                selectionColor: root.accent
                font.family: root.monoFamily
                font.pixelSize: 13
            }
        }
    }

    Shortcut { sequence: "Ctrl+L"; context: Qt.WindowShortcut; onActivated: root.newSession() }
    Shortcut { sequence: "Ctrl+D"; context: Qt.WindowShortcut; onActivated: root.toggleDrawer() }
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
                Item {
                    width: 10
                    height: 10
                    anchors.verticalCenter: parent.verticalCenter
                    Rectangle {
                        id: brandDot
                        objectName: "brandDot"
                        anchors.centerIn: parent
                        width: 6
                        height: 6
                        radius: 3
                        readonly property bool busy: bridge ? bridge.busy : false
                        color: "#38bdf8"
                        opacity: 0.85
                        layer.enabled: true
                        SequentialAnimation {
                            running: brandDot.busy
                            loops: Animation.Infinite
                            onRunningChanged: if (!running) brandDot.opacity = 0.85
                            NumberAnimation { target: brandDot; property: "opacity"; from: 0.85; to: 0.35; duration: 700; easing.type: Easing.InOutSine }
                            NumberAnimation { target: brandDot; property: "opacity"; from: 0.35; to: 0.85; duration: 700; easing.type: Easing.InOutSine }
                        }
                    }
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
                    text: lockButton.active ? "\uD83D\uDD12\uFE0E" : "\uD83D\uDD13\uFE0E"
                    color: lockButton.active ? "#f59e0b" : Qt.rgba(1, 1, 1, 0.25)
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
                ToolTip.text: "Toggle Private Egress"
            }
        }

        ListView {
            id: list
            Layout.fillWidth: true
            Layout.fillHeight: true
            model: transcript
            clip: true
            spacing: 10
            topMargin: 14
            bottomMargin: 14
            boundsBehavior: Flickable.StopAtBounds
            ScrollBar.vertical: ScrollBar { }

            // Follow the stream unless the user scrolled away from the bottom.
            property bool follow: true
            onMovementEnded: follow = atYEnd
            onContentHeightChanged: if (follow) Qt.callLater(positionViewAtEnd)
            onCountChanged: { follow = true; Qt.callLater(positionViewAtEnd) }

            Label {
                anchors.centerIn: parent
                visible: transcript.count === 0
                text: "Ask Strand anything"
                color: root.dim
                font.pixelSize: 15
            }

            // Asymmetric turn geometry: the user is a compact, recessed, right-aligned card;
            // Strand is a left-aligned slate card (max 85% / 820px); notes are dim local asides.
            delegate: Item {
                id: row
                required property string role
                required property string body
                required property bool pending
                required property string trail
                readonly property bool isUser: role === "user"
                width: list.width
                height: bubble.height

                Item {
                    id: bubble
                    objectName: "bubble"
                    readonly property bool isNote: row.role === "note"
                    // One-way geometry: bubble.width derives only from row.width and text metrics,
                    // never from `content` (which wraps to bubble.width), so there are no layout loops.
                    readonly property bool isThinking: row.pending && row.body.length === 0
                    readonly property bool isAssistant: row.role === "assistant"
                    readonly property bool boxed: row.isUser || isAssistant
                    readonly property real hPad: boxed ? root.bodyPixelSize : 0
                    readonly property real vPad: boxed ? Math.round(root.bodyPixelSize * 0.7) : 2
                    readonly property real edge: Math.round(root.bodyPixelSize * 8 / 7)
                    readonly property real maxWidth: row.isUser ? row.width * 0.72
                        : (isAssistant ? Math.min(row.width * 0.85, 820, row.width - 2 * edge) : row.width - 2 * edge)
                    TextMetrics {
                        id: textMetrics
                        font.pixelSize: root.bodyPixelSize
                        text: (bubble.isThinking ? "\u2026" : row.body)
                    }
                    readonly property real naturalWidth: Math.ceil(textMetrics.advanceWidth) + 2 * hPad + 2
                    x: row.isUser ? row.width - width - edge : edge
                    // Notes use the full column; user cards and the assistant thinking pill shrink to fit;
                    // assistant answers fill their bounded card width.
                    width: (row.isUser || (isAssistant && isThinking)) ? Math.min(maxWidth, naturalWidth) : Math.max(0, maxWidth)
                    height: content.implicitHeight + 2 * vPad

                    Rectangle {
                        anchors.fill: parent
                        visible: bubble.boxed
                        radius: 12
                        color: row.isUser ? root.userRecess : "#13171f"
                        border.color: row.isUser ? root.userBorder : Qt.rgba(1, 1, 1, 0.06)
                        border.width: 1
                    }

                    Column {
                        id: content
                        objectName: "content"
                        x: bubble.hPad
                        y: bubble.vPad
                        width: bubble.width - 2 * bubble.hPad
                        spacing: 8

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
                            text: row.body
                            color: bubble.isNote ? root.dim : root.textColor
                            font.italic: bubble.isNote
                            selectionColor: root.accent
                            font.pixelSize: root.bodyPixelSize
                        }

                        // Assistant replies: markdown narrative blocks and fenced technical cards.
                        Repeater {
                            model: !bridge || row.isUser || bubble.isNote ? [] : bridge.segments(row.pending && row.body.length === 0 ? "…" : row.body)
                            delegate: Loader {
                                required property var modelData
                                width: content.width
                                sourceComponent: modelData.code ? codeBlock : textBlock
                                property var segment: modelData
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

            // Natural width of the longest typed line, used to stretch the dock horizontally.
            TextMetrics {
                id: typedMetrics
                font: input.font
                text: {
                    var lines = input.text.split("\n")
                    var longest = ""
                    for (var i = 0; i < lines.length; i++)
                        if (lines[i].length > longest.length)
                            longest = lines[i]
                    return longest
                }
            }

            Rectangle {
                id: dock
                objectName: "inputDock"
                readonly property real restingWidth: Math.min(Math.max(500, Math.min(footer.width * 0.65, 780)), footer.width - 24)
                readonly property real maxWidth: Math.max(restingWidth, Math.min(footer.width - 96, 920))
                readonly property real wantedWidth: typedMetrics.advanceWidth + input.leftPadding + input.rightPadding + 8
                readonly property real verticalPadding: input.topPadding + input.bottomPadding
                anchors.horizontalCenter: parent.horizontalCenter
                anchors.top: parent.top
                anchors.topMargin: 8
                width: Math.min(Math.max(restingWidth, wantedWidth), maxWidth)
                // Tracks the wrapped text height: one line (44) up to 180, then scrolls inside.
                height: Math.min(Math.max(input.contentHeight + verticalPadding, 44), 180)
                radius: 14
                color: "#13171f"
                border.width: 1
                border.color: input.activeFocus ? "#38bdf8" : Qt.rgba(1, 1, 1, 0.08)
                // Animate only when the input is empty (resting/reset). While typing the width
                // snaps, so the wrap engine and an in-flight animation never race (BUG-UI-001).
                Behavior on width {
                    enabled: input.text.length === 0
                    NumberAnimation { duration: 150; easing.type: Easing.OutQuad }
                }

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
                        enabled: bridge ? !bridge.busy : false
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
                    ScrollBar.vertical: ScrollBar { }
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
        x: open ? parent.width - width - 12 : parent.width
        visible: x < parent.width
        clip: true
        radius: 16
        // Near-opaque #0d1117 surface (0.96) so transcript text never ghosts through.
        color: Qt.rgba(0.051, 0.067, 0.090, 0.96)
        border.color: Qt.rgba(1, 1, 1, 0.08)
        border.width: 1
        Behavior on x { NumberAnimation { duration: 140; easing.type: Easing.OutCubic } }

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
        visible: root.settingsOpen
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

                    // Panel A: bedrock persona stance
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
                                text: "PERSONA ANCHOR"
                                color: root.textColor
                                font.family: root.uiFamily
                                font.pixelSize: 12
                                font.bold: true
                                font.letterSpacing: 1.0
                            }
                            Label {
                                Layout.fillWidth: true
                                text: "The core identity injected on Turn 1 to override corporate boilerplate."
                                wrapMode: Text.WordWrap
                                color: root.dim
                                opacity: 0.8
                                font.family: root.uiFamily
                                font.pixelSize: 11
                            }
                            ScrollView {
                                Layout.fillWidth: true
                                Layout.fillHeight: true
                                Layout.minimumHeight: 90
                                TextArea {
                                    id: personaEdit
                                    objectName: "personaEdit"
                                    color: root.textColor
                                    selectionColor: root.accent
                                    font.family: root.monoFamily
                                    font.pixelSize: 12
                                    wrapMode: TextEdit.Wrap
                                    leftPadding: 10
                                    rightPadding: 10
                                    topPadding: 8
                                    bottomPadding: 8
                                    placeholderText: "Core stance injected ahead of the personality matrix"
                                    background: Rectangle { radius: 6; color: "#090b0e"; border.color: root.codeBorder; border.width: 1 }
                                }
                            }
                            RowLayout {
                                Layout.fillWidth: true
                                spacing: 12
                                Label {
                                    id: stanceStatus
                                    objectName: "stanceStatus"
                                    Layout.fillWidth: true
                                    horizontalAlignment: Text.AlignRight
                                    text: root.stanceMessage
                                    color: root.stanceMessage === "Warp anchored" ? root.diffAdded : root.diffRemoved
                                    font.pixelSize: 11
                                }
                                SleekButton {
                                    objectName: "saveStance"
                                    text: "Save Stance"
                                    onClicked: root.stanceMessage = root.persist({ persona_prompt: personaEdit.text }) ? "Warp anchored" : "Save failed"
                                }
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
                                id: failClosedBox
                                objectName: "failClosedBox"
                                text: "Sovereign Fiber Isolation"
                                onToggled: root.persist({ fail_closed_private: checked })
                            }
                            ConsequenceLabel {
                                active: failClosedBox.checked
                                warnUnchecked: true
                                checkedText: "Quarantine private strands locally. Never sent to external LLMs unless unlocked via /private."
                                uncheckedText: "Warning: Private strands will be transmitted to external LLMs whenever relevant."
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
                                checkedText: "Scrub high-entropy API keys, tokens, and certs before storing or sending to external models."
                                uncheckedText: "Warning: Raw credentials and tokens will be saved to disk and may leak to external LLMs."
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
                                background: Rectangle { radius: 6; color: "#090b0e"; border.color: root.codeBorder; border.width: 1 }
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
                                background: Rectangle { radius: 6; color: "#090b0e"; border.color: root.codeBorder; border.width: 1 }
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
                                background: Rectangle { radius: 6; color: "#090b0e"; border.color: root.codeBorder; border.width: 1 }
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
                                background: Rectangle { radius: 6; color: "#090b0e"; border.color: root.codeBorder; border.width: 1 }
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
                            TextField {
                                id: extractorModelEdit
                                objectName: "extractorModelEdit"
                                Layout.fillWidth: true
                                placeholderText: "e.g. qwen2.5:3b or llama3.2:3b"
                                color: root.textColor
                                selectionColor: root.accent
                                font.family: root.monoFamily
                                font.pixelSize: 12
                                leftPadding: 10
                                rightPadding: 10
                                background: Rectangle { radius: 6; color: "#090b0e"; border.color: root.codeBorder; border.width: 1 }
                                onEditingFinished: root.persist({ extractor_model: text })
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
                                        root.persist({ extractor_url: extractorUrlEdit.text, extractor_model: extractorModelEdit.text })
                                        root.extractorOk = false
                                        root.extractorMessage = "Testing\u2026"
                                        bridge.testExtractor(extractorUrlEdit.text)
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
