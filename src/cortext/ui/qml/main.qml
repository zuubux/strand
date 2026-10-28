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
    title: "Cortext"
    color: "#16191D"

    readonly property color panel: "#1E2228"
    readonly property color border: "#2C323A"
    readonly property color userBubble: "#2B5C8A"
    readonly property color botBubble: "#232830"
    readonly property color textColor: "#E6E9ED"
    readonly property color dim: "#7F8A96"
    readonly property color accent: "#3DAEE9"

    ListModel { id: transcript }

    function send() {
        var text = input.text.trim()
        if (text.length === 0 || bridge.busy)
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
        function onBusyChanged() { if (!bridge.busy) input.forceActiveFocus() }
    }

    ColumnLayout {
        anchors.fill: parent
        anchors.margins: 1
        spacing: 0

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
                text: "Ask Cortext anything"
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
                    x: row.isUser ? row.width - width - 16 : 16
                    width: Math.min(row.width * 0.82, Math.max(60, content.implicitWidth + 28))
                    height: content.implicitHeight + 20
                    radius: 12
                    color: row.isUser ? root.userBubble : root.botBubble
                    border.color: row.isUser ? "transparent" : root.border

                    TextEdit {
                        id: content
                        x: 14
                        y: 10
                        width: bubble.width - 28
                        readOnly: true
                        selectByMouse: true
                        wrapMode: TextEdit.Wrap
                        textFormat: row.isUser ? TextEdit.PlainText : TextEdit.MarkdownText
                        text: row.pending && row.body.length === 0 ? "…" : row.body
                        color: root.textColor
                        selectionColor: root.accent
                        font.pixelSize: 14
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
                        placeholderText: "Message Cortext…  (Enter to send, Shift+Enter for newline)"
                        placeholderTextColor: root.dim
                        color: root.textColor
                        wrapMode: TextArea.Wrap
                        font.pixelSize: 14
                        enabled: !bridge.busy
                        background: null

                        Keys.onPressed: function(event) {
                            if ((event.key === Qt.Key_Return || event.key === Qt.Key_Enter)
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

    Rectangle {
        anchors.fill: parent
        color: "transparent"
        border.color: root.border
        border.width: 1
    }

    Component.onCompleted: input.forceActiveFocus()
}
