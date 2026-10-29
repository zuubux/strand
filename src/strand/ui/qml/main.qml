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
    readonly property color codeBg: "#0E1114"
    readonly property color codeBorder: "#3A4452"
    readonly property string monoFamily: "monospace"

    readonly property int bodyPixelSize: 14

    property bool drawerOpen: false
    // True while Up/Down is stepping through previously sent prompts.
    property bool browsingHistory: false

    ListModel { id: transcript }

    function note(message) {
        transcript.append({ role: "note", body: message, pending: false })
    }

    function newSession() {
        // The bridge aborts a running turn, rotates session_id and emits conversationReset.
        bridge.newConversation()
        input.text = ""
        input.forceActiveFocus()
    }

    function toggleDrawer() { drawerOpen = !drawerOpen }

    // Esc: stop the running stream; otherwise clear the input.
    function handleEscape() {
        if (bridge.busy)
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
                    transcript.append({ role: "assistant", body: "", pending: true })
                return
            }
        }
        if (bridge.busy)
            return
        transcript.append({ role: "user", body: text, pending: false })
        transcript.append({ role: "assistant", body: "", pending: true })
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
        function onBusyChanged() { if (!bridge.busy) input.forceActiveFocus() }
    }

    Component {
        id: textBlock
        TextEdit {
            readOnly: true
            selectByMouse: true
            wrapMode: TextEdit.Wrap
            textFormat: TextEdit.MarkdownText
            text: segment.text
            color: root.textColor
            selectionColor: root.accent
            font.pixelSize: root.bodyPixelSize
        }
    }

    Component {
        id: codeBlock
        Rectangle {
            implicitHeight: codeText.implicitHeight + (langLabel.visible ? langLabel.height : 0) + 16
            implicitWidth: codeText.implicitWidth + 20
            radius: 6
            color: root.codeBg
            border.color: root.codeBorder
            Label {
                id: langLabel
                visible: segment.lang.length > 0
                x: 10
                y: 4
                text: segment.lang
                color: root.accent
                font.family: root.monoFamily
                font.pixelSize: 10
            }
            TextEdit {
                id: codeText
                x: 10
                y: (langLabel.visible ? langLabel.height + 4 : 0) + 8
                width: parent.width - 20
                readOnly: true
                selectByMouse: true
                wrapMode: TextEdit.WrapAnywhere
                textFormat: TextEdit.PlainText
                text: segment.text
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
            Layout.preferredHeight: 26
            color: "#12151A"
            RowLayout {
                anchors.fill: parent
                anchors.leftMargin: 12
                anchors.rightMargin: 6
                Label {
                    text: bridge.privateMode ? "Strand  ·  private" : "Strand"
                    color: root.dim
                    font.pixelSize: 11
                }
                Item { Layout.fillWidth: true }
                ToolButton {
                    id: drawerButton
                    objectName: "drawerButton"
                    Layout.preferredWidth: 26
                    Layout.preferredHeight: 22
                    text: "ⓘ"
                    opacity: root.drawerOpen || hovered ? 1.0 : 0.55
                    onClicked: root.toggleDrawer()
                    ToolTip.visible: hovered
                    ToolTip.text: "Diagnostics (Ctrl+D)"
                    contentItem: Label {
                        text: drawerButton.text
                        color: root.drawerOpen ? root.accent : root.dim
                        horizontalAlignment: Text.AlignHCenter
                        verticalAlignment: Text.AlignVCenter
                        font.pixelSize: 14
                    }
                    background: null
                }
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

            delegate: Item {
                id: row
                required property string role
                required property string body
                required property bool pending
                readonly property bool isUser: role === "user"
                width: list.width
                height: bubble.height

                Rectangle {
                    id: bubble
                    objectName: "bubble"
                    readonly property bool isNote: row.role === "note"
                    // One-way geometry: bubble.width derives only from row.width and text metrics,
                    // never from `content` (which wraps to bubble.width), so there are no layout loops.
                    readonly property bool isThinking: row.pending && row.body.length === 0
                    readonly property real hPad: root.bodyPixelSize
                    readonly property real vPad: Math.round(root.bodyPixelSize * 0.7)
                    readonly property real edge: Math.round(root.bodyPixelSize * 8 / 7)
                    readonly property real maxWidth: row.width * 0.82
                    // Replies with fenced code keep the full responsive width; the measured text
                    // width cannot account for monospace blocks.
                    readonly property bool hasCode: !row.isUser && row.body.indexOf("```") >= 0
                    TextMetrics {
                        id: textMetrics
                        font.pixelSize: root.bodyPixelSize
                        text: bubble.isNote ? "" : (bubble.isThinking ? "\u2026" : row.body)
                    }
                    readonly property real naturalWidth: Math.ceil(textMetrics.advanceWidth) + 2 * hPad + 2
                    x: isNote ? edge : (row.isUser ? row.width - width - edge : edge)
                    width: isNote ? Math.max(0, row.width - 2 * edge)
                         : (hasCode ? maxWidth : Math.min(maxWidth, naturalWidth))
                    height: content.implicitHeight + 2 * vPad
                    radius: 12
                    color: isNote ? "transparent" : (row.isUser ? root.userBubble : root.botBubble)
                    border.color: row.isUser || isNote ? "transparent" : root.border

                    Column {
                        id: content
                        objectName: "content"
                        x: bubble.hPad
                        y: bubble.vPad
                        width: bubble.width - 2 * bubble.hPad
                        spacing: 6

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

                        // Assistant replies: markdown text blocks and fenced code blocks.
                        Repeater {
                            model: row.isUser || bubble.isNote ? [] : bridge.segments(row.pending && row.body.length === 0 ? "…" : row.body)
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

        Rectangle { Layout.fillWidth: true; Layout.preferredHeight: 1; color: root.border }

        Rectangle {
            Layout.fillWidth: true
            Layout.preferredHeight: inputFlick.height + 24
            color: root.panel

            Rectangle {
                anchors.fill: parent
                anchors.margins: 10
                radius: 10
                color: "#14171B"
                border.color: input.activeFocus ? root.accent : root.border

                Flickable {
                    id: inputFlick
                    anchors.left: parent.left
                    anchors.right: parent.right
                    anchors.top: parent.top
                    anchors.margins: 2
                    contentWidth: width
                    contentHeight: input.implicitHeight
                    clip: true
                    boundsBehavior: Flickable.StopAtBounds
                    // Auto-expands with content, from one line up to ~8.
                    height: Math.min(Math.max(input.implicitHeight, 40), 180)

                    TextArea.flickable: TextArea {
                        id: input
                        objectName: "messageInput"
                        placeholderText: "Message Strand…  (Enter to send, Shift+Enter for newline)"
                        placeholderTextColor: root.dim
                        color: root.textColor
                        wrapMode: TextArea.Wrap
                        font.pixelSize: 14
                        // Fixed alignment + padding: the placeholder inherits these, so the
                        // text baseline never moves on focus/blur/typing.
                        verticalAlignment: Text.AlignTop
                        topPadding: 10
                        bottomPadding: 10
                        leftPadding: 8
                        rightPadding: 8
                        enabled: !bridge.busy
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
            }
        }

        Rectangle {
            Layout.fillWidth: true
            Layout.preferredHeight: 24
            color: "#12151A"

            RowLayout {
                anchors.fill: parent
                anchors.leftMargin: 12
                anchors.rightMargin: 12
                spacing: 6

                Rectangle {
                    Layout.preferredWidth: 8
                    Layout.preferredHeight: 8
                    radius: 4
                    color: bridge.status.indexOf("Connected") === 0 ? "#2ECC71"
                         : bridge.status === "Disconnected" ? "#E5534B" : root.dim
                }
                Label { text: bridge.status; color: root.dim; font.pixelSize: 11 }
                Item { Layout.fillWidth: true }
                Label { text: bridge.modelName; color: root.dim; font.pixelSize: 11 }
            }
        }
    }

    Rectangle { Layout.fillHeight: true; Layout.preferredWidth: root.drawerOpen ? 1 : 0; color: root.border }

    Rectangle {
        id: drawer
        objectName: "drawer"
        Layout.fillHeight: true
        Layout.preferredWidth: root.drawerOpen ? 280 : 0
        visible: Layout.preferredWidth > 0
        clip: true
        color: root.panel
        Behavior on Layout.preferredWidth { NumberAnimation { duration: 140; easing.type: Easing.OutCubic } }

        Column {
            x: 14
            y: 14
            width: 252
            spacing: 10
            Label { text: "Diagnostics"; color: root.textColor; font.pixelSize: 14; font.bold: true }
            Repeater {
                model: [
                    ["Session", bridge.diagnostics.sessionId],
                    ["Model", bridge.diagnostics.model],
                    ["Transport", bridge.diagnostics.transport],
                    ["Status", bridge.diagnostics.status],
                    ["Private context", bridge.diagnostics.private ? "on" : "off"],
                    ["Last turn", bridge.diagnostics.outcome.length > 0 ? bridge.diagnostics.outcome : "—"],
                    ["Prompt chars", String(bridge.diagnostics.promptChars)],
                    ["Reply chars", String(bridge.diagnostics.replyChars)],
                    ["First token", bridge.diagnostics.firstTokenMs + " ms"],
                    ["Round trip", bridge.diagnostics.latencyMs + " ms"]
                ]
                delegate: Column {
                    required property var modelData
                    width: 252
                    spacing: 1
                    Label { text: modelData[0]; color: root.dim; font.pixelSize: 10 }
                    TextEdit {
                        width: parent.width
                        readOnly: true
                        selectByMouse: true
                        wrapMode: TextEdit.WrapAnywhere
                        text: modelData[1]
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

    Rectangle {
        anchors.fill: parent
        color: "transparent"
        border.color: root.border
        border.width: 1
    }

    Component.onCompleted: input.forceActiveFocus()
}
