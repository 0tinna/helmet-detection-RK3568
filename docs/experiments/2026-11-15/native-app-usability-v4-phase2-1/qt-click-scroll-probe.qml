import QtQuick 2.15
import QtQuick.Controls 2.15
import QtQuick.Layouts 1.15
import QtQuick.Window 2.15

ApplicationWindow {
    id: probe
    width: Screen.width
    height: Screen.height
    visibility: Window.FullScreen
    visible: true
    color: "#101e2a"
    title: "Qt scroll click probe"

    property int upperClicks: 0
    property int bottomClicks: 0

    ScrollView {
        anchors.fill: parent
        clip: true
        contentWidth: availableWidth

        ColumnLayout {
            width: parent.width
            spacing: 14

            Button {
                Layout.fillWidth: true
                implicitHeight: 100
                text: "UPPER " + probe.upperClicks
                font.pixelSize: 30
                onClicked: probe.upperClicks += 1
            }

            Item {
                Layout.fillWidth: true
                Layout.preferredHeight: 1040
            }

            Button {
                Layout.fillWidth: true
                implicitHeight: 64
                text: "BOTTOM " + probe.bottomClicks
                font.pixelSize: 30
                onClicked: probe.bottomClicks += 1
            }
        }
    }
}
