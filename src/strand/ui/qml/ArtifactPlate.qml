import QtQuick
import QtQuick.Controls

// Unboxed breakout plate for fenced code, diffs and structured output. It spans the full
// reading column handed to it by the transcript Loader and never inherits bubble styling.
Rectangle {
    id: plate
    objectName: "artifactPlate"
    // "code" or "diff"; `lang` is the fence language tag (may be empty).
    property string plateType: "code"
    property string lang: ""
    property string text: ""
    readonly property bool isDiff: plateType === "diff"
    readonly property string filename: isDiff ? diffFilename(text) : ""
    readonly property string badge: filename.length > 0 ? filename
        : (lang.length > 0 ? lang : (isDiff ? "diff" : "snippet"))

    implicitHeight: header.height + body.implicitHeight + 2 * Theme.vPadding
    height: implicitHeight
    implicitWidth: parent ? parent.width : 320
    width: parent ? parent.width : implicitWidth
    radius: Theme.artifactRadius
    color: Theme.artifactBackground
    border.color: Theme.artifactBorder
    border.width: 1
    clip: true

    function diffFilename(src) {
        var m = /^\+\+\+ (?:b\/)?(\S+)/m.exec(src)
        if (m && m[1] !== "/dev/null")
            return m[1]
        m = /^--- (?:a\/)?(\S+)/m.exec(src)
        return m && m[1] !== "/dev/null" ? m[1] : ""
    }

    function escapeHtml(s) {
        return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    }

    function diffHtml(src) {
        var out = []
        var lines = src.split("\n")
        for (var i = 0; i < lines.length; i++) {
            var line = lines[i]
            var color = Theme.codeText
            if (line.indexOf("+++") === 0 || line.indexOf("---") === 0)
                color = Theme.textMuted
            else if (line.indexOf("@@") === 0)
                color = Theme.brandAccent
            else if (line.charAt(0) === "+")
                color = Theme.successAccent
            else if (line.charAt(0) === "-")
                color = Theme.dangerAccent
            out.push("<span style=\"color:" + color + "\">" + escapeHtml(line) + "</span>")
        }
        return "<pre style=\"margin:0\">" + out.join("\n") + "</pre>"
    }

    function copy() {
        if (typeof bridge !== "undefined" && bridge)
            bridge.copyToClipboard(plate.text)
    }

    Rectangle {
        id: header
        objectName: "artifactHeader"
        width: parent.width
        height: Theme.artifactHeaderHeight
        color: Theme.artifactHeaderBackground
        Label {
            objectName: "artifactBadge"
            anchors.left: parent.left
            anchors.leftMargin: Theme.artifactPadding
            anchors.right: copyButton.left
            anchors.rightMargin: 8
            anchors.verticalCenter: parent.verticalCenter
            text: plate.badge
            elide: Text.ElideMiddle
            color: Theme.brandAccent
            font.family: Theme.monoFontFamily
            font.pixelSize: Theme.captionPixelSize
        }
        Item {
            id: copyButton
            objectName: "artifactCopy"
            anchors.right: parent.right
            anchors.rightMargin: Theme.artifactPadding
            anchors.verticalCenter: parent.verticalCenter
            width: copyLabel.implicitWidth
            height: parent.height
            property bool copied: false
            Label {
                id: copyLabel
                anchors.verticalCenter: parent.verticalCenter
                text: copyButton.copied ? "[ Copied ]" : "[ Copy ]"
                color: copyArea.containsMouse ? Theme.codeText : Theme.textMuted
                font.family: Theme.monoFontFamily
                font.pixelSize: Theme.captionPixelSize
            }
            Timer {
                id: copiedReset
                interval: 1500
                onTriggered: copyButton.copied = false
            }
            MouseArea {
                id: copyArea
                anchors.fill: parent
                hoverEnabled: true
                cursorShape: Qt.PointingHandCursor
                onClicked: {
                    plate.copy()
                    copyButton.copied = true
                    copiedReset.restart()
                }
            }
        }
        Rectangle { anchors.bottom: parent.bottom; width: parent.width; height: 1; color: Theme.artifactBorder }
    }

    TextEdit {
        id: body
        objectName: "artifactBody"
        x: Theme.artifactPadding
        y: header.height + Theme.vPadding
        width: parent.width - 2 * Theme.artifactPadding
        readOnly: true
        selectByMouse: true
        wrapMode: TextEdit.WrapAnywhere
        textFormat: plate.isDiff ? TextEdit.RichText : TextEdit.PlainText
        text: plate.isDiff ? plate.diffHtml(plate.text) : plate.text
        color: Theme.codeText
        selectionColor: Theme.brandAccent
        font.family: Theme.monoFontFamily
        font.pixelSize: Theme.codePixelSize
    }
}
