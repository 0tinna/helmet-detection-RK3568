import QtQuick 2.15
import QtQuick.Controls 2.15
import QtQuick.Layouts 1.15

ApplicationWindow {
    id: window
    width: 720
    height: 1120
    visible: true
    title: "安全帽检测与 Wi-Fi"
    color: "#101e2a"
    property string apiBase: ""
    property string apiToken: ""
    property string initialTab: "detection"
    property var modelChoices: []
    property var sourceChoices: []
    property var profileChoices: []
    property var networks: []
    property var detection: ({})
    property var wifiStatus: ({})
    property var actionState: ({busy:false})
    property string message: "正在连接本地检测服务"
    property string wifiMessage: "Wi-Fi 仅用于需要时联网，检测可以离线运行"
    property bool haveConfig: false
    property bool statusPending: false
    property bool framePending: false
    property int previewEpoch: 0
    property double frameExpires: 0
    property bool networkSupported: true
    property var keyboardTarget: null
    property bool uppercase: false
    property bool symbols: false
    property bool keyboardOpen: false
    property double lastStatus: 0
    property string lastActionId: ""
    property bool passwordVisible: false
    property bool automaticDemo: false
    property int smokeSeconds: 0
    property string verificationDir: ""
    property bool verificationKeyboard: false
    property bool verificationScan: false

    function request(method, route, body, callback) {
        var xhr = new XMLHttpRequest()
        xhr.open(method, apiBase + route)
        xhr.setRequestHeader("X-Helmet-Token", apiToken)
        if (method === "POST") xhr.setRequestHeader("Content-Type", "application/json")
        xhr.onreadystatechange = function() {
            if (xhr.readyState === XMLHttpRequest.DONE) {
                var value = {}
                try { value = JSON.parse(xhr.responseText) } catch(e) {}
                callback(xhr.status >= 200 && xhr.status < 300, value)
            }
        }
        xhr.send(method === "POST" ? JSON.stringify(body || {}) : null)
    }
    function chooseIndex(items, id) {
        for (var i = 0; i < items.length; i++) if (items[i].id === id) return i
        return 0
    }
    function configure() {
        request("GET", "/api/config", null, function(ok, value) {
            if (!ok) { message = "本地服务暂不可用，请重新打开应用"; return }
            modelChoices = value.models || []; sourceChoices = value.sources || []
            profileChoices = value.profiles || []
            modelChoice.currentIndex = chooseIndex(modelChoices, value.defaults.model)
            sourceChoice.currentIndex = chooseIndex(sourceChoices, value.defaults.source)
            profileChoice.currentIndex = chooseIndex(profileChoices, value.defaults.profile)
            haveConfig = true
            message = "选择输入后开始检测。录像是回放演示，摄像头需实际接入"
            if (automaticDemo) startDetection()
        })
    }
    function startDetection() {
        if (!haveConfig || !modelChoices.length || !sourceChoices.length) return
        message = "正在启动检测"
        request("POST", "/api/start", {
            model:modelChoices[modelChoice.currentIndex].id,
            source:sourceChoices[sourceChoice.currentIndex].id,
            profile:profileChoices[profileChoice.currentIndex].id
        }, function(ok, value) { message = ok ? "检测已启动" : "启动失败：" + (value.error || "请检查模型与输入") })
    }
    function stopDetection() {
        message = "正在停止检测"
        invalidatePreview()
        request("POST", "/api/stop", {}, function(ok, value) {
            message = ok && value.stopped ? "检测已停止" : "尚未完成停止，请等待工作线程退出"
        })
    }
    function updateWifi() {
        request("GET", "/api/wifi/status", null, function(ok, value) {
            if (ok) wifiStatus = value
            else wifiMessage = "无法读取无线状态，请检查无线模块"
        })
    }
    function poll() {
        if (statusPending) return
        statusPending = true
        request("GET", "/api/status", null, function(ok, value) {
            statusPending = false
            if (!ok) { invalidatePreview(); message="本地服务不可用"; return }
            lastStatus = Date.now()
            var previousGeneration = detection.generation
            var previousSourceEpoch = detection.source_epoch
            detection = value.detection || {}
            if (previousGeneration !== detection.generation || previousSourceEpoch !== detection.source_epoch) invalidatePreview()
            actionState = value.wifi_action || {busy:false}
            if (!detection.valid || !detection.running) invalidatePreview()
            if (actionState.id && actionState.id !== lastActionId && !actionState.busy) {
                lastActionId = actionState.id
                if (actionState.kind === "scan" && actionState.status === "completed") {
                    networks = actionState.result ? (actionState.result.networks || []) : []
                    wifiMessage = networks.length ? "选择网络，输入密码后点击连接" : "没有发现可用网络，可输入隐藏网络名称"
                } else if (actionState.kind === "connect") {
                    var result = actionState.result || {}
                    wifiMessage = result.connected ? "无线连接成功" + (result.ip ? "，IP " + result.ip : "") : "连接未完成，请检查密码、信号与认证方式"
                    if (result.connected && remember.checked && result.saved === false) wifiMessage += "；网络配置未保存"
                    password.text = ""
                    updateWifi()
                } else if (actionState.status === "failed") wifiMessage = "操作失败，请检查无线模块和网络设置"
            }
        })
    }
    function scanWifi() {
        wifiMessage = "正在扫描附近 Wi-Fi"
        request("POST", "/api/wifi/scan", {}, function(ok, value) {
            if (!ok) wifiMessage="扫描未启动，可能已有无线操作正在进行"
        })
    }
    function connectWifi() {
        wifiMessage = "正在连接，请等待。切换网络会重新获取地址"
        request("POST", "/api/wifi/connect", {
            ssid:ssid.text, password:password.text,
            security:security.currentIndex === 0 ? "WPA2-PSK" : "open", remember:remember.checked
        }, function(ok, value) { if (!ok) wifiMessage="连接未启动，请检查输入或等待当前操作完成" })
        password.focus = false
        keyboardOpen = false
    }
    function refreshFrame() {
        if (!detection.valid || !detection.running || Date.now()-lastStatus>1500) {
            invalidatePreview()
            return
        }
        if (framePending) return
        framePending = true
        var epoch = previewEpoch
        var started = Date.now()
        request("GET", "/api/preview", null, function(ok, value) {
            framePending = false
            if (epoch !== previewEpoch) return
            var remaining = Number(value.remaining_ttl_ms || 0) - (Date.now()-started)
            if (!ok || !value.valid || !detection.valid || !detection.running || remaining<=0
                    || value.generation !== detection.generation || value.source_epoch !== detection.source_epoch) {
                invalidatePreview()
                return
            }
            preview.visible = false
            frameExpires = Date.now()+remaining
            preview.source = "data:image/jpeg;base64," + value.jpeg_base64
        })
    }
    function invalidatePreview() {
        previewEpoch += 1
        frameExpires = 0
        preview.visible = false
        preview.source = ""
    }

    header: ColumnLayout {
        spacing: 8
        Rectangle {
            Layout.fillWidth: true; height: 86; color: "#173247"
            RowLayout {
                anchors.fill: parent; anchors.margins: 18; spacing: 16
                Image { source:"../icons/helmet.svg"; sourceSize.width:52; sourceSize.height:52; Layout.preferredWidth:52; Layout.preferredHeight:52 }
                Column {
                    Text { text:"安全帽检测"; color:"white"; font.pixelSize:27; font.bold:true }
                    Text { text:"RK3568 本地运行"; color:"#b9d0df"; font.pixelSize:17 }
                }
                Item { Layout.fillWidth:true }
                Label { text:detection.running ? "运行中" : "待机"; color:detection.running ? "#93e0ad" : "#cbd8e1"; font.pixelSize:21 }
            }
        }
        TabBar {
            id: tabs; Layout.fillWidth:true; currentIndex:initialTab === "wifi" ? 1 : 0
            TabButton { text:"检测"; font.pixelSize:22; implicitHeight:58 }
            TabButton { text:"Wi-Fi 设置"; font.pixelSize:22; implicitHeight:58 }
            onCurrentIndexChanged: if (currentIndex===1) updateWifi()
        }
    }
    Item {
        id: screenRoot
        anchors.fill:parent
    StackLayout {
        id: contentRoot
        anchors.fill: parent; anchors.margins:18; currentIndex:tabs.currentIndex
        ScrollView {
            clip:true; contentWidth:availableWidth
            ColumnLayout {
                width:parent.width; spacing:14
                Label { text:message; color:"#c9dce8"; wrapMode:Text.Wrap; Layout.fillWidth:true; font.pixelSize:19 }
                Rectangle {
                    Layout.fillWidth:true; Layout.preferredHeight:Math.min(window.height*0.34,400); color:"#07131d"; radius:10
                    Image {
                        id: preview; anchors.fill:parent; anchors.margins:5; fillMode:Image.PreserveAspectFit
                        asynchronous:true; cache:false; visible:false
                        onStatusChanged: {
                            if (status===Image.Ready || status===Image.Error) {
                                visible=status===Image.Ready && detection.valid && detection.running && Date.now()-lastStatus<=1500 && Date.now()<frameExpires
                            }
                        }
                    }
                    Label { anchors.centerIn:parent; text:detection.running ? "等待新鲜检测画面" : "点击开始检测"; color:"#a5bfce"; font.pixelSize:23; visible:!preview.visible }
                }
                Label {
                    text:(detection.state || "idle") + (detection.reason ? "　"+detection.reason : "")
                    color:detection.valid ? "#91dfa9" : "#f0c389"; wrapMode:Text.Wrap; Layout.fillWidth:true; font.pixelSize:19
                }
                Label {
                    text:"检测 FPS（滑窗）：" + (detection.fps && detection.fps.detected !== undefined ? Number(detection.fps.detected).toFixed(2) : "—") + "　帧号：" + (detection.frame_id || "—")
                    color:"white"; font.pixelSize:20; wrapMode:Text.Wrap; Layout.fillWidth:true
                }
                Label { text:"模型"; color:"#a9c2d1"; font.pixelSize:18 }
                ComboBox { id:modelChoice; model:modelChoices; textRole:"name"; Layout.fillWidth:true; implicitHeight:55; font.pixelSize:19; enabled:!detection.running }
                Label { text:"输入源"; color:"#a9c2d1"; font.pixelSize:18 }
                ComboBox { id:sourceChoice; model:sourceChoices; textRole:"name"; Layout.fillWidth:true; implicitHeight:55; font.pixelSize:19; enabled:!detection.running }
                ComboBox { id:profileChoice; model:profileChoices; textRole:"name"; Layout.fillWidth:true; implicitHeight:55; font.pixelSize:19; enabled:!detection.running }
                RowLayout {
                    Layout.fillWidth:true; spacing:15
                    Button { text:"开始检测"; Layout.fillWidth:true; implicitHeight:64; font.pixelSize:23; enabled:haveConfig && !detection.running; onClicked:startDetection(); background:Rectangle{color:parent.enabled?"#237b96":"#334855";radius:6} contentItem:Text{text:parent.text;color:parent.enabled?"white":"#a8bdc9";font:parent.font;horizontalAlignment:Text.AlignHCenter;verticalAlignment:Text.AlignVCenter} }
                    Button { text:"停止"; Layout.fillWidth:true; implicitHeight:64; font.pixelSize:23; enabled:!!detection.running; onClicked:stopDetection(); background:Rectangle{color:parent.enabled?"#237b96":"#334855";radius:6} contentItem:Text{text:parent.text;color:parent.enabled?"white":"#a8bdc9";font:parent.font;horizontalAlignment:Text.AlignHCenter;verticalAlignment:Text.AlignVCenter} }
                }
                Label { text:"空检测不能说明全员戴帽。断流或过期时隐藏旧画面。回放可离线演示，摄像头接入后需单独验收。"; Layout.fillWidth:true; wrapMode:Text.Wrap; color:"#96afbf"; font.pixelSize:17 }
            }
        }
        ScrollView {
            id: wifiScroll; clip:true; contentWidth:availableWidth
            ColumnLayout {
                width:wifiScroll.availableWidth; spacing:13
                RowLayout {
                    Image { source:"../icons/wifi.svg"; sourceSize.width:40; sourceSize.height:40; Layout.preferredWidth:40; Layout.preferredHeight:40 }
                    Label { text: wifiStatus.ssid ? "当前网络："+wifiStatus.ssid : "无线未连接"; color:"white"; font.pixelSize:22; wrapMode:Text.Wrap; Layout.fillWidth:true }
                }
                Label { text:(wifiStatus.state || "未知状态") + (wifiStatus.ip ? "　IP："+wifiStatus.ip : ""); color:"#aac4d4"; font.pixelSize:18; wrapMode:Text.Wrap; Layout.fillWidth:true }
                Button { text:actionState.busy ? "操作中…" : "扫描附近 Wi-Fi"; Layout.fillWidth:true; implicitHeight:60; font.pixelSize:22; enabled:!actionState.busy; onClicked:scanWifi() }
                ScrollView {
                    Layout.fillWidth:true; Layout.preferredHeight:Math.min(210,networks.length*64)
                    visible:networks.length>0; clip:true; contentWidth:availableWidth
                    ColumnLayout {
                        width:parent.width
                        Repeater {
                            model:networks
                            Button {
                                Layout.fillWidth:true; implicitHeight:60
                                text:modelData.ssid + "　" + modelData.signal + " dBm　" + modelData.security
                                font.pixelSize:18; enabled:!actionState.busy
                                onClicked:{ ssid.text=modelData.ssid; networkSupported=modelData.security === "open" || modelData.security === "WPA2-PSK"; security.currentIndex=modelData.security === "open" ? 1 : 0; wifiMessage=networkSupported ? "输入密码后连接" : "该网络认证方式暂不支持，不能按开放网络连接" }
                            }
                        }
                    }
                }
                Label { text:"网络名称（也可输入隐藏网络）"; color:"#a9c2d1"; font.pixelSize:18 }
                TextField { id:ssid; Layout.fillWidth:true; implicitHeight:56; placeholderText:"SSID"; font.pixelSize:21; enabled:!actionState.busy; maximumLength:32; onTextEdited:networkSupported=true; onActiveFocusChanged:if(activeFocus){keyboardTarget=ssid;keyboardOpen=true} }
                ComboBox { id:security; Layout.fillWidth:true; implicitHeight:54; model:["WPA2 个人密码网络", "开放网络（无密码）"]; font.pixelSize:19; enabled:!actionState.busy }
                TextField {
                    id:password; Layout.fillWidth:true; implicitHeight:58; placeholderText:"Wi-Fi 密码"
                    font.pixelSize:21; echoMode:passwordVisible ? TextInput.Normal : TextInput.Password
                    inputMethodHints:Qt.ImhSensitiveData | Qt.ImhNoPredictiveText | Qt.ImhLatinOnly
                    enabled:!actionState.busy && security.currentIndex===0; maximumLength:64
                    onActiveFocusChanged:if(activeFocus){keyboardTarget=password;keyboardOpen=true}
                }
                RowLayout {
                    Layout.fillWidth:true
                    CheckBox { text:"显示密码"; font.pixelSize:18; onToggled:passwordVisible=checked }
                    CheckBox { id:remember; text:"保存网络"; font.pixelSize:18; enabled:!actionState.busy; checked:false }
                }
                Button { text:"连接所选网络"; Layout.fillWidth:true; implicitHeight:64; font.pixelSize:23; enabled:!actionState.busy && networkSupported && ssid.text.length>0; onClicked:connectWifi(); background:Rectangle{color:parent.enabled?"#237b96":"#334855";radius:6} contentItem:Text{text:parent.text;color:parent.enabled?"white":"#a8bdc9";font:parent.font;horizontalAlignment:Text.AlignHCenter;verticalAlignment:Text.AlignVCenter} }
                Label { text:wifiMessage; color:"#e5c594"; wrapMode:Text.Wrap; Layout.fillWidth:true; font.pixelSize:19 }
                Label { text:"支持开放网络与 WPA2 个人网络。企业认证与 WPA3 暂不支持。密码仅用于本机连接；切换网络可能中断远程连接，离线检测不受联网条件限制。"; color:"#96afbf"; wrapMode:Text.Wrap; Layout.fillWidth:true; font.pixelSize:17 }
                Item { Layout.preferredHeight:keyboardOpen ? keyboard.height : 0 }
            }
        }
    }
    Rectangle {
        id:keyboard; z:100; width:parent.width; height:300; color:"#20394d"
        y:parent.height-height; visible:keyboardOpen && tabs.currentIndex===1
        ColumnLayout {
            anchors.fill:parent; anchors.margins:8; spacing:5
            Repeater {
                model: symbols ? ["! @ # $ % ^ & * ( )", "- _ = + [ ] { } / ?", "~ ` : ; , . \\ | < >", "\" '"] : ["1 2 3 4 5 6 7 8 9 0", "q w e r t y u i o p", "a s d f g h j k l", "z x c v b n m"]
                RowLayout {
                    Layout.fillWidth:true; spacing:4
                    Repeater {
                        model:modelData.split(" ")
                        Button {
                            text:uppercase ? modelData.toUpperCase() : modelData
                            Layout.fillWidth:true; Layout.fillHeight:true; font.pixelSize:24
                            focusPolicy:Qt.NoFocus
                            onClicked:if(keyboardTarget){keyboardTarget.insert(keyboardTarget.cursorPosition,text);if(keyboardTarget===ssid)networkSupported=true}
                        }
                    }
                }
            }
            RowLayout {
                Layout.fillWidth:true; spacing:5
                Button { text:uppercase ? "小写" : "大写"; Layout.fillWidth:true; focusPolicy:Qt.NoFocus; onClicked:uppercase=!uppercase }
                Button { text:symbols ? "字母" : "符号"; Layout.fillWidth:true; focusPolicy:Qt.NoFocus; onClicked:symbols=!symbols }
                Button { text:"空格"; Layout.fillWidth:true; focusPolicy:Qt.NoFocus; onClicked:if(keyboardTarget)keyboardTarget.insert(keyboardTarget.cursorPosition," ") }
                Button { text:"退格"; Layout.fillWidth:true; focusPolicy:Qt.NoFocus; onClicked:if(keyboardTarget&&keyboardTarget.cursorPosition>0)keyboardTarget.remove(keyboardTarget.cursorPosition-1,keyboardTarget.cursorPosition) }
                Button { text:"完成"; Layout.fillWidth:true; focusPolicy:Qt.NoFocus; onClicked:keyboardOpen=false }
            }
        }
    }
    }
    Timer { interval:500; repeat:true; running:true; onTriggered:poll() }
    Timer { interval:200; repeat:true; running:tabs.currentIndex===0; onTriggered:refreshFrame() }
    Timer { interval:100; repeat:true; running:true; onTriggered:if (Date.now()-lastStatus>1500 || Date.now()>=frameExpires) preview.visible=false }
    Timer { interval:5000; repeat:true; running:tabs.currentIndex===1; onTriggered:updateWifi() }
    Timer { interval:smokeSeconds*1000; running:smokeSeconds>0; onTriggered:Qt.quit() }
    Timer { interval:1000; running:verificationScan; onTriggered:scanWifi() }
    Timer {
        interval:Math.max(1000,(smokeSeconds-2)*1000); running:smokeSeconds>2 && verificationDir.length>0
        onTriggered: {
            if(verificationKeyboard){keyboardTarget=password;keyboardOpen=true}
            screenRoot.grabToImage(function(result){
                var saved=result.saveToFile(verificationDir+"/screen.png")
                console.log("APP_RENDER",JSON.stringify({saved:saved,tab:tabs.currentIndex,valid:!!detection.valid,previewVisible:preview.visible,frameId:detection.frame_id||null,keyboardVisible:keyboard.visible,width:window.width,height:window.height}))
            })
        }
    }
    Component.onCompleted: configure()
    onClosing: { invalidatePreview(); request("POST","/api/stop",{},function(ok,value){});Qt.quit() }
}
