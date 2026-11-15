import QtQuick 2.15
import QtQuick.Controls 2.15
import QtQuick.Layouts 1.15
import QtQuick.Window 2.15

ApplicationWindow {
    id: window
    width: Screen.width
    height: Screen.height
    visibility: Window.FullScreen
    visible: !residentMode
    title: "安全帽检测与 Wi-Fi"
    color: "#101e2a"
    property string apiBase: ""
    property string apiToken: ""
    property string initialTab: "detection"
    property bool residentMode: false
    property string appSession: ""
    property string uiInstance: ""
    property var application: ({})
    property bool appStopping: false
    property bool appPending: false
    property bool applyingIntent: false
    property int appliedSequence: -1
    property int requestCounter: 0
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
    property bool configPending: false
    property int configEpoch: 0
    property bool statusPending: false
    property bool framePending: false
    property bool frameLoadPending: false
    property var previewLoadTarget: null
    property bool previewSwapPending: false
    property var previewSwapTarget: null
    property int previewSwapSerial: 0
    property int previewEpoch: 0
    property int frameSerial: 0
    property var frameRequest: null
    property double frameExpires: 0
    property var preview: previewA
    property bool previewVisible: false
    property double minimumCaptureAt: 0
    property var previewFrameId: null
    property double previewCapturedAt: 0
    property string previewGeneration: ""
    property var previewSourceEpoch: null
    property var previewSelection: ({})
    property int frameRequests: 0
    property int frameDecodes: 0
    property int previewPresentations: 0
    property bool networkSupported: true
    property var keyboardTarget: null
    property bool uppercase: false
    property bool symbols: false
    property bool keyboardOpen: false
    property double lastStatus: 0
    property string lastActionId: ""
    property bool passwordVisible: false
    property bool automaticDemo: false
    property bool automaticDemoStarted: false
    property int smokeSeconds: 0
    property string verificationDir: ""
    property bool verificationKeyboard: false
    property bool verificationScan: false
    property bool verificationResidentUi: false
    property bool capturePending: false
    property int captureSequence: -1

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
        return xhr
    }
    function nextRequestId() {
        requestCounter += 1
        return "ui-" + uiInstance + "-" + requestCounter
    }
    function chooseIndex(items, id) {
        for (var i = 0; i < items.length; i++) if (items[i].id === id) return i
        return -1
    }
    function selectionIds() {
        return {
            model: modelChoice.currentIndex >= 0 && modelChoice.currentIndex < modelChoices.length ? modelChoices[modelChoice.currentIndex].id : null,
            source: sourceChoice.currentIndex >= 0 && sourceChoice.currentIndex < sourceChoices.length ? sourceChoices[sourceChoice.currentIndex].id : null,
            profile: profileChoice.currentIndex >= 0 && profileChoice.currentIndex < profileChoices.length ? profileChoices[profileChoice.currentIndex].id : null
        }
    }
    function applyConfiguration(value) {
        var previous = selectionIds()
        var first = !haveConfig
        var actual = value.detection || detection
        if (actual.generation !== detection.generation || actual.source_epoch !== detection.source_epoch
                || !actual.running || !actual.valid) invalidatePreview()
        var models = (value.models || []).slice()
        var sources = (value.sources || []).slice()
        var profiles = (value.profiles || []).slice()
        detection = actual
        var selected = actual.running ? (actual.selection || {}) : first ? (value.defaults || {}) : previous
        function runningIndex(items, id) {
            var index = chooseIndex(items, id)
            if (actual.running && index < 0 && id) {
                items.push({id:id, name:id + "（当前设备不可用）", available:false})
                index = items.length - 1
            }
            return index
        }
        var modelIndex = runningIndex(models, selected.model)
        var sourceIndex = runningIndex(sources, selected.source)
        var profileIndex = runningIndex(profiles, selected.profile)
        modelChoices = models; sourceChoices = sources; profileChoices = profiles
        // Assign indexes after model replacement; a missing idle input stays -1.
        modelChoice.currentIndex = modelIndex
        sourceChoice.currentIndex = sourceIndex
        profileChoice.currentIndex = profileIndex
        haveConfig = true
        if (actual.running) {
            message = sourceChoices[sourceChoice.currentIndex] && sourceChoices[sourceChoice.currentIndex].available === false
                    ? "当前运行输入已不可用，仍保留真实配置；停止后可刷新并重新选择"
                    : "已恢复当前检测配置；回到桌面会继续检测"
        } else {
            message = canStart() ? "选择输入后开始检测。录像是回放演示，摄像头需实际接入"
                                 : "原输入或模型已不可用，请明确重新选择后开始"
        }
    }
    function canStart() {
        if (!haveConfig || configPending || detection.running || appStopping) return false
        return modelChoice.currentIndex >= 0 && modelChoice.currentIndex < modelChoices.length
                && sourceChoice.currentIndex >= 0 && sourceChoice.currentIndex < sourceChoices.length
                && profileChoice.currentIndex >= 0 && profileChoice.currentIndex < profileChoices.length
                && modelChoices[modelChoice.currentIndex].available !== false
                && sourceChoices[sourceChoice.currentIndex].available !== false
                && profileChoices[profileChoice.currentIndex].available !== false
    }
    function configure() {
        if (configPending || appStopping) return
        configPending = true
        configEpoch += 1
        var epoch = configEpoch
        request("GET", "/api/config", null, function(ok, value) {
            if (epoch !== configEpoch) return
            configPending = false
            if (!ok) { message = "本地服务暂不可用，请重新打开应用"; return }
            applyConfiguration(value)
            if (automaticDemo && !residentMode && !automaticDemoStarted) {
                automaticDemoStarted = true
                startDetection()
            }
        })
    }
    function startDetection() {
        if (!canStart()) return
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
        if (!window.visible || appStopping) return
        request("GET", "/api/wifi/status", null, function(ok, value) {
            if (ok) wifiStatus = value
            else wifiMessage = "无法读取无线状态，请检查无线模块"
        })
    }
    function poll() {
        if (statusPending) return
        statusPending = true
        var sequence = appliedSequence
        request("GET", "/api/status", null, function(ok, value) {
            if (residentMode && sequence !== appliedSequence) return
            statusPending = false
            if (!ok) { invalidatePreview(); message="本地服务不可用"; return }
            lastStatus = Date.now()
            var previousGeneration = detection.generation
            var previousSourceEpoch = detection.source_epoch
            detection = value.detection || {}
            if (value.service && value.service.state === "stopping") appStopping = true
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
        if (appStopping) return
        wifiMessage = "正在扫描附近 Wi-Fi"
        request("POST", "/api/wifi/scan", {}, function(ok, value) {
            if (!ok) wifiMessage="扫描未启动，可能已有无线操作正在进行"
        })
    }
    function connectWifi() {
        if (appStopping) return
        wifiMessage = "正在连接，请等待。切换网络会重新获取地址"
        request("POST", "/api/wifi/connect", {
            ssid:ssid.text, password:password.text,
            security:security.currentIndex === 0 ? "WPA2-PSK" : "open", remember:remember.checked
        }, function(ok, value) { if (!ok) wifiMessage="连接未启动，请检查输入或等待当前操作完成" })
        password.focus = false
        keyboardOpen = false
    }
    function settlePreview(image, imageStatus) {
        if (imageStatus !== Image.Ready && imageStatus !== Image.Error) return
        if (image === previewLoadTarget) {
            frameLoadPending = false
            previewLoadTarget = null
        }
        if (imageStatus === Image.Ready && image.source.toString().length > 0)
            frameDecodes = Math.min(2147483647, frameDecodes + 1)
        var fresh = imageStatus === Image.Ready && window.visible && !appStopping && tabs.currentIndex===0
                && detection.valid && detection.running && Date.now()-lastStatus<=1500
                && image.pendingEpoch === previewEpoch && Date.now() < image.pendingExpires
                && image.pendingGeneration === detection.generation
                && image.pendingSourceEpoch === detection.source_epoch
                && image.pendingCapturedAt >= minimumCaptureAt
        if (!fresh) {
            image.opacity = 0
            image.z = 0
            return
        }
        var other = image === previewA ? previewB : previewA
        image.opacity = 1
        image.z = 2
        other.z = 1
        preview = image
        previewVisible = true
        frameExpires = image.pendingExpires
        previewFrameId = image.pendingFrameId
        previewCapturedAt = image.pendingCapturedAt
        previewGeneration = image.pendingGeneration
        previewSourceEpoch = image.pendingSourceEpoch
        previewSelection = JSON.parse(JSON.stringify(image.pendingSelection || {}))
        previewPresentations = Math.min(2147483647, previewPresentations + 1)
        previewSwapSerial += 1
        previewSwapTarget = other
        previewSwapPending = true
        previewHideTimer.restart()
        captureShow()
    }
    function finishPreviewSwap(image, serial) {
        if (!previewSwapPending || serial !== previewSwapSerial) return
        image.opacity = 0
        image.z = 0
        preview.z = 0
        previewSwapPending = false
        previewSwapTarget = null
    }
    function refreshFrame() {
        if (capturePending) return
        if (!window.visible || tabs.currentIndex !== 0 || appStopping) { invalidatePreview(); return }
        if (!detection.valid || !detection.running || Date.now()-lastStatus>1500) {
            invalidatePreview()
            return
        }
        if (framePending || frameLoadPending) return
        framePending = true
        frameRequests = Math.min(2147483647, frameRequests + 1)
        frameSerial += 1
        var serial = frameSerial
        var epoch = previewEpoch
        var started = Date.now()
        var target = preview === previewA ? previewB : previewA
        frameRequest = request("GET", "/api/preview?after=" + minimumCaptureAt, null, function(ok, value) {
            if (epoch !== previewEpoch || serial !== frameSerial) return
            framePending = false
            frameRequest = null
            if (!window.visible || capturePending || appStopping) return
            var remaining = Number(value.remaining_ttl_ms || 0) - (Date.now()-started)
            if (!ok || !value.valid || !detection.valid || !detection.running || remaining<=0
                    || value.generation !== detection.generation || value.source_epoch !== detection.source_epoch
                    || typeof value.captured_at !== "number" || value.captured_at < minimumCaptureAt) {
                invalidatePreview()
                return
            }
            target.pendingEpoch = epoch
            target.pendingExpires = Date.now()+remaining
            target.pendingFrameId = value.frame_id
            target.pendingCapturedAt = value.captured_at
            target.pendingGeneration = value.generation
            target.pendingSourceEpoch = value.source_epoch
            target.pendingSelection = JSON.parse(JSON.stringify(detection.selection || {}))
            frameLoadPending = true
            previewLoadTarget = target
            target.source = "data:image/jpeg;base64," + value.jpeg_base64
        })
    }
    function invalidatePreview() {
        previewEpoch += 1
        frameSerial += 1
        previewSwapSerial += 1
        if (frameRequest && frameRequest.abort) frameRequest.abort()
        frameRequest = null
        framePending = false
        frameLoadPending = false
        previewLoadTarget = null
        previewSwapPending = false
        previewSwapTarget = null
        previewHideTimer.stop()
        frameExpires = 0
        previewVisible = false
        previewA.opacity = 0
        previewA.z = 0
        previewB.opacity = 0
        previewB.z = 0
        previewA.source = ""
        previewB.source = ""
        preview = previewA
    }
    function clearUiSecrets() {
        password.text = ""
        passwordVisible = false
        keyboardOpen = false
        keyboardTarget = null
        password.focus = false
    }
    function ackApplication() {
        if (!residentMode || appliedSequence < 0) return
        request("POST", "/api/application/ack", {
            session:appSession, request_id:nextRequestId(), ui_instance:uiInstance,
            sequence:appliedSequence, visible:window.visible,
            tab:tabs.currentIndex === 1 ? "wifi" : "detection",
            frame_requests:frameRequests, frame_decodes:frameDecodes,
            preview_presentations:previewPresentations
        }, function(ok, value) {})
    }
    function applyApplication(value) {
        if (!value || value.session !== appSession) return
        appStopping = appStopping || value.phase === "stopping" || value.phase === "failed" || value.phase === "completed"
        if (appStopping) {
            invalidatePreview()
            message = "正在退出应用，等待设备与无线操作完成正常清理"
        }
        var intent = value.intent
        if (!intent || intent.sequence < appliedSequence) return
        application = value
        if (intent.sequence === appliedSequence) { ackApplication(); return }
        invalidatePreview()
        configEpoch += 1; configPending = false
        statusPending = false; lastStatus = 0
        appliedSequence = intent.sequence
        minimumCaptureAt = Number(intent.preview_after || 0)
        applyingIntent = true
        tabs.currentIndex = intent.tab === "wifi" ? 1 : 0
        applyingIntent = false
        if (intent.visible && !appStopping) {
            window.showFullScreen(); window.raise(); window.requestActivate()
            configure(); poll()
            if (tabs.currentIndex === 1) updateWifi()
        } else {
            clearUiSecrets()
            window.hide()
        }
        ackApplication()
    }
    function pollApplication() {
        if (!residentMode || appPending) return
        appPending = true
        request("GET", "/api/application", null, function(ok, value) {
            appPending = false
            if (ok) applyApplication(value)
            else { invalidatePreview(); message = "后台状态暂不可用，不能确认已退出" }
        })
    }
    function hideApplication() {
        if (!residentMode) { window.close(); return }
        invalidatePreview()
        clearUiSecrets()
        window.hide()
        request("POST", "/api/application/hide", {session:appSession, request_id:nextRequestId()},
                function(ok, value) { if (ok) applyApplication(value.application); else message = "隐藏请求未确认，后台检测仍由服务持有" })
    }
    function quitApplication() {
        if (!residentMode) { window.close(); return }
        appStopping = true
        invalidatePreview()
        clearUiSecrets()
        message = "正在退出应用，等待设备与无线操作完成正常清理"
        request("POST", "/api/application/quit", {session:appSession, request_id:nextRequestId()},
                function(ok, value) { if (ok) applyApplication(value.application); else message = "退出尚未确认，请等待后台正常清理" })
    }
    function captureShow() {
        if (!verificationResidentUi || !verificationDir || capturePending || captureSequence === appliedSequence
                || appliedSequence < 0 || !window.visible || appStopping || !haveConfig || configPending
                || statusPending || Date.now()-lastStatus>1500 || password.text.length > 0) return
        if (tabs.currentIndex === 0 && detection.running && !previewVisible) return
        captureSequence = appliedSequence
        capturePending = true
        var sequence = appliedSequence
        var epoch = previewEpoch
        var captureId = "show-" + sequence + "-" + uiInstance
        var packet = previewVisible ? {frame_id:previewFrameId, captured_at:previewCapturedAt,
            generation:previewGeneration, source_epoch:previewSourceEpoch,
            selection:JSON.parse(JSON.stringify(previewSelection))} : null
        frameSerial += 1
        if (frameRequest && frameRequest.abort) frameRequest.abort()
        frameRequest = null; framePending = false
        var captureRoot = window.contentItem.parent
        captureRoot.grabToImage(function(result) {
            var fresh = epoch === previewEpoch && sequence === appliedSequence && window.visible && !appStopping
            if (packet) fresh = fresh && previewVisible && Date.now() < frameExpires
                    && previewFrameId === packet.frame_id && previewGeneration === packet.generation
                    && previewSourceEpoch === packet.source_epoch && detection.valid
            var saved = fresh ? result.saveToFile(verificationDir + "/" + captureId + ".png") : false
            capturePending = false
            request("POST", "/api/application/render", {session:appSession, request_id:nextRequestId(),
                ui_instance:uiInstance, sequence:sequence, visible:window.visible,
                tab:tabs.currentIndex === 1 ? "wifi" : "detection", saved:saved,
                capture_id:captureId, width:window.width, height:window.height, packet:packet
            }, function(ok, value) {
                var receipt = ok && value.application ? value.application.last_render : null
                console.log("APP_RESIDENT_RENDER", JSON.stringify({capture_id:captureId, saved:saved,
                    sequence:sequence, ui_instance:uiInstance, backend_receipt:receipt,
                    frame_requests:frameRequests, frame_decodes:frameDecodes,
                    preview_presentations:previewPresentations}))
            })
        })
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
                Label { text:appStopping ? "退出中" : detection.running ? "运行中" : "待机"; color:detection.running ? "#93e0ad" : "#cbd8e1"; font.pixelSize:21 }
            }
        }
        RowLayout {
            Layout.fillWidth:true; Layout.leftMargin:18; Layout.rightMargin:18; spacing:12
            Button { text:"回到桌面"; Layout.fillWidth:true; implicitHeight:52; font.pixelSize:20; enabled:!appStopping; onClicked:hideApplication() }
            Button { text:"退出应用"; Layout.fillWidth:true; implicitHeight:52; font.pixelSize:20; enabled:!appStopping; onClicked:quitApplication() }
        }
        TabBar {
            id: tabs; Layout.fillWidth:true; currentIndex:initialTab === "wifi" ? 1 : 0
            TabButton { text:"检测"; font.pixelSize:22; implicitHeight:58 }
            TabButton { text:"Wi-Fi 设置"; font.pixelSize:22; implicitHeight:58 }
            onCurrentIndexChanged: {
                if (currentIndex === 1 && window.visible) updateWifi()
                if (residentMode && window.visible && !applyingIntent && !appStopping) {
                    request("POST", "/api/application/show", {session:appSession, request_id:nextRequestId(),
                        tab:currentIndex === 1 ? "wifi" : "detection"},
                        function(ok, value) { if (ok) applyApplication(value.application) })
                }
            }
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
                        id: previewA; anchors.fill:parent; anchors.margins:5; fillMode:Image.PreserveAspectFit
                        asynchronous:true; cache:true; opacity:0
                        property int pendingEpoch: -1
                        property double pendingExpires: 0
                        property int pendingFrameId: -1
                        property double pendingCapturedAt: 0
                        property string pendingGeneration: ""
                        property var pendingSourceEpoch: null
                        property var pendingSelection: ({})
                        onStatusChanged: settlePreview(previewA, status)
                    }
                    Image {
                        id: previewB; anchors.fill:parent; anchors.margins:5; fillMode:Image.PreserveAspectFit
                        asynchronous:true; cache:true; opacity:0
                        property int pendingEpoch: -1
                        property double pendingExpires: 0
                        property int pendingFrameId: -1
                        property double pendingCapturedAt: 0
                        property string pendingGeneration: ""
                        property var pendingSourceEpoch: null
                        property var pendingSelection: ({})
                        onStatusChanged: settlePreview(previewB, status)
                    }
                    Label { anchors.centerIn:parent; text:"点击开始检测"; color:"#a5bfce"; font.pixelSize:23; visible:!detection.running }
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
                ComboBox { id:modelChoice; model:modelChoices; textRole:"name"; Layout.fillWidth:true; implicitHeight:55; font.pixelSize:19; enabled:!detection.running && !appStopping }
                Label { text:"输入源"; color:"#a9c2d1"; font.pixelSize:18 }
                ComboBox { id:sourceChoice; model:sourceChoices; textRole:"name"; Layout.fillWidth:true; implicitHeight:55; font.pixelSize:19; enabled:!detection.running && !appStopping }
                ComboBox { id:profileChoice; model:profileChoices; textRole:"name"; Layout.fillWidth:true; implicitHeight:55; font.pixelSize:19; enabled:!detection.running && !appStopping }
                Button { text:configPending ? "正在刷新输入…" : "刷新输入"; Layout.fillWidth:true; implicitHeight:52; font.pixelSize:20; enabled:!detection.running && !appStopping && !configPending; onClicked:configure() }
                RowLayout {
                    Layout.fillWidth:true; spacing:15
                    Button { text:"开始检测"; Layout.fillWidth:true; implicitHeight:64; font.pixelSize:23; enabled:canStart(); onClicked:startDetection(); background:Rectangle{color:parent.enabled?"#237b96":"#334855";radius:6} contentItem:Text{text:parent.text;color:parent.enabled?"white":"#a8bdc9";font:parent.font;horizontalAlignment:Text.AlignHCenter;verticalAlignment:Text.AlignVCenter} }
                    Button { text:"停止检测"; Layout.fillWidth:true; implicitHeight:64; font.pixelSize:23; enabled:!!detection.running && !appStopping; onClicked:stopDetection(); background:Rectangle{color:parent.enabled?"#237b96":"#334855";radius:6} contentItem:Text{text:parent.text;color:parent.enabled?"white":"#a8bdc9";font:parent.font;horizontalAlignment:Text.AlignHCenter;verticalAlignment:Text.AlignVCenter} }
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
                Button { text:actionState.busy ? "操作中…" : "扫描附近 Wi-Fi"; Layout.fillWidth:true; implicitHeight:60; font.pixelSize:22; enabled:!actionState.busy && !appStopping; onClicked:scanWifi() }
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
                                font.pixelSize:18; enabled:!actionState.busy && !appStopping
                                onClicked:{ ssid.text=modelData.ssid; networkSupported=modelData.security === "open" || modelData.security === "WPA2-PSK"; security.currentIndex=modelData.security === "open" ? 1 : 0; wifiMessage=networkSupported ? "输入密码后连接" : "该网络认证方式暂不支持，不能按开放网络连接" }
                            }
                        }
                    }
                }
                Label { text:"网络名称（也可输入隐藏网络）"; color:"#a9c2d1"; font.pixelSize:18 }
                TextField { id:ssid; Layout.fillWidth:true; implicitHeight:56; placeholderText:"SSID"; font.pixelSize:21; enabled:!actionState.busy && !appStopping; maximumLength:32; onTextEdited:networkSupported=true; onActiveFocusChanged:if(activeFocus){keyboardTarget=ssid;keyboardOpen=true} }
                ComboBox { id:security; Layout.fillWidth:true; implicitHeight:54; model:["WPA2 个人密码网络", "开放网络（无密码）"]; font.pixelSize:19; enabled:!actionState.busy && !appStopping }
                TextField {
                    id:password; Layout.fillWidth:true; implicitHeight:58; placeholderText:"Wi-Fi 密码"
                    font.pixelSize:21; echoMode:passwordVisible ? TextInput.Normal : TextInput.Password
                    inputMethodHints:Qt.ImhSensitiveData | Qt.ImhNoPredictiveText | Qt.ImhLatinOnly
                    enabled:!actionState.busy && !appStopping && security.currentIndex===0; maximumLength:64
                    onActiveFocusChanged:if(activeFocus){keyboardTarget=password;keyboardOpen=true}
                }
                RowLayout {
                    Layout.fillWidth:true
                    CheckBox { text:"显示密码"; font.pixelSize:18; checked:passwordVisible; enabled:!appStopping; onToggled:passwordVisible=checked }
                    CheckBox { id:remember; text:"保存网络"; font.pixelSize:18; enabled:!actionState.busy && !appStopping; checked:false }
                }
                Button { text:"连接所选网络"; Layout.fillWidth:true; implicitHeight:64; font.pixelSize:23; enabled:!actionState.busy && !appStopping && networkSupported && ssid.text.length>0; onClicked:connectWifi(); background:Rectangle{color:parent.enabled?"#237b96":"#334855";radius:6} contentItem:Text{text:parent.text;color:parent.enabled?"white":"#a8bdc9";font:parent.font;horizontalAlignment:Text.AlignHCenter;verticalAlignment:Text.AlignVCenter} }
                Label { text:wifiMessage; color:"#e5c594"; wrapMode:Text.Wrap; Layout.fillWidth:true; font.pixelSize:19 }
                Label { text:"支持开放网络与 WPA2 个人网络。企业认证与 WPA3 暂不支持。密码仅用于本机连接；切换网络可能中断远程连接，离线检测不受联网条件限制。"; color:"#96afbf"; wrapMode:Text.Wrap; Layout.fillWidth:true; font.pixelSize:17 }
                Item { Layout.preferredHeight:keyboardOpen ? keyboard.height : 0 }
            }
        }
    }
    Rectangle {
        id:keyboard; z:100; width:parent.width; height:300; color:"#20394d"
        y:parent.height-height; visible:window.visible && !appStopping && keyboardOpen && tabs.currentIndex===1
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
    Timer { interval:window.visible ? 500 : 1500; repeat:true; running:true; onTriggered:poll() }
    Timer { interval:window.visible ? 500 : 1000; repeat:true; running:residentMode; onTriggered:pollApplication() }
    Timer { interval:125; repeat:true; running:window.visible && tabs.currentIndex===0 && !appStopping; onTriggered:refreshFrame() }
    Timer {
        id: previewHideTimer; interval:100; repeat:false
        onTriggered:finishPreviewSwap(previewSwapTarget, previewSwapSerial)
    }
    Timer { interval:100; repeat:true; running:window.visible; onTriggered:if ((Date.now()-lastStatus>1500 || Date.now()>=frameExpires) && preview.source.toString().length>0) invalidatePreview() }
    Timer { interval:5000; repeat:true; running:window.visible && tabs.currentIndex===1 && !appStopping; onTriggered:updateWifi() }
    Timer { interval:400; repeat:true; running:verificationResidentUi && window.visible && !appStopping; onTriggered:captureShow() }
    Timer { interval:smokeSeconds*1000; running:smokeSeconds>0; onTriggered:Qt.quit() }
    Timer { interval:1000; running:verificationScan; onTriggered:scanWifi() }
    Timer {
        interval:Math.max(1000,(smokeSeconds-2)*1000); running:smokeSeconds>2 && verificationDir.length>0
        onTriggered: {
            if(verificationKeyboard){keyboardTarget=password;keyboardOpen=true}
            screenRoot.grabToImage(function(result){
                var saved=result.saveToFile(verificationDir+"/screen.png")
                console.log("APP_RENDER",JSON.stringify({saved:saved,tab:tabs.currentIndex,valid:!!detection.valid,previewVisible:previewVisible,frameId:detection.frame_id||null,keyboardVisible:keyboard.visible,width:window.width,height:window.height}))
            })
        }
    }
    Component.onCompleted: {
        if (residentMode) pollApplication()
        else { configure(); poll() }
    }
    onClosing: function(close) {
        if (residentMode) { close.accepted = false; hideApplication() }
        else { invalidatePreview(); clearUiSecrets(); request("POST","/api/stop",{},function(ok,value){}); Qt.quit() }
    }
}
