import QtQuick 2.15
import QtQuick.Window 2.15

Window {
    id: probe
    width: 720
    height: 1280
    visible: true
    visibility: Window.FullScreen
    color: "#102030"
    title: "Touch coordinate probe"

    property real pressX: -1
    property real pressY: -1
    property int pressCount: 0

    Timer {
        interval: 300
        running: true
        repeat: true
        onTriggered: {
            probe.showFullScreen()
            probe.raise()
            probe.requestActivate()
        }
    }

    Rectangle {
        anchors.fill: parent
        color: "#102030"
    }

    Text {
        anchors.horizontalCenter: parent.horizontalCenter
        y: 40
        color: "white"
        font.pixelSize: 28
        text: "Weston 收到的触摸坐标"
    }

    Text {
        anchors.centerIn: parent
        color: "#93e0ad"
        font.pixelSize: 34
        text: probe.pressCount
              ? "x=" + probe.pressX.toFixed(1) + "  y=" + probe.pressY.toFixed(1)
              : "尚未收到触摸"
    }

    Rectangle {
        visible: probe.pressCount > 0
        x: probe.pressX - 18
        y: probe.pressY - 18
        width: 36
        height: 36
        radius: 18
        color: "#ffb000"
    }

    MouseArea {
        anchors.fill: parent
        acceptedButtons: Qt.LeftButton
        onPressed: {
            probe.pressX = mouse.x
            probe.pressY = mouse.y
            probe.pressCount += 1
            console.log("TOUCH_COORDINATE", mouse.x, mouse.y)
        }
    }
}
