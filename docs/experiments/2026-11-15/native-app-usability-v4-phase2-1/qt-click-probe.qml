import QtQuick 2.15
import QtQuick.Controls 2.15
import QtQuick.Window 2.15

ApplicationWindow {
    id: probe
    width: Screen.width
    height: Screen.height
    visibility: Window.FullScreen
    visible: true
    color: "#101e2a"
    title: "Qt click probe"

    property int upperClicks: 0
    property int bottomClicks: 0

    Button {
        x: 20
        y: 300
        width: 680
        height: 100
        text: "UPPER " + probe.upperClicks
        font.pixelSize: 30
        onClicked: probe.upperClicks += 1
    }

    Button {
        x: 20
        y: 1160
        width: 680
        height: 80
        text: "BOTTOM " + probe.bottomClicks
        font.pixelSize: 30
        onClicked: probe.bottomClicks += 1
    }
}
