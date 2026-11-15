# v3 原生窗口正常关闭收据

2026-11-15。该实验补齐的是已安装 v3 App 的普通桌面图标启动和标题栏关闭路径。裁决为 **PASS_LIMITED**：它支持实际 vendor wrapper、实际 QML 窗口、正常 compositor 输入路径以及 `service_exit=0 / ui_exit=0 / cleanup_completed=true`，但不能转写成 v4 常驻 App、真实手指触摸、关窗后继续检测或 1800 秒验收。

## 执行路径

- 从桌面安全帽图标启动已安装 v3。实际入口为 `/opt/ui/src/apps/helmet_app`，SHA256 为 `cf5523c368fd518ba879e15e68bef1be20462ecb3fec802d72bf34679fb98c40`，继续进入历史目录名 `/userdata/helmet-validation/20261115-native-app-v2` 中的 `launch.py`、`service.py` 和 `Main.qml`。
- 运行 session 为 `oix00y_8`。启动器 PID/starttime 为 `2760/9310944`，服务 `2765/9311296`，Qt `2767/9311506`。这三组 starttime 是运行期操作记录；已复制的 `processes.json` 只保留 PID。
- 通过临时 `/dev/uinput` 绝对指针，在 720×1280 屏幕坐标 `(710, 88)` 点击浮动窗口标题栏关闭按钮。设备声明 `INPUT_PROP_POINTER` 和 `BTN_LEFT`，事件经正常 Weston/libinput/compositor 路径进入 v3 窗口。
- 同轮诊断中，纠正后的 MT-B 直连触摸已能命中“检测/WiFi”标签；相对指针在标题栏位置曾成功最小化同一窗口。这些是输入坐标和作用窗口的旁证，不是人体手指验收。

## 终态

`exit.json` 原样记录：

```json
{"service_exit": 0, "ui_exit": 0, "cleanup_completed": true}
```

板端原目录 `/userdata/helmet-validation/v3-normal-close-success-20261115T112458Z` 与本地副本的 `exit.json` SHA256 同为 `0156cebfe71769b4b26948f5f4a708f16c032c3376b520a2db1c45d210126bfd`。运行前后确认 launcher/service/Qt 三个原始进程均已退出。

此前的 session `8m34zlm3` 曾出现 `ui_exit=-15`。该失败历史继续保留，不能使用本收据覆盖。本收据也不把“进程退出 0”扩大成直接 SDK release、无泄漏或所有未来清理路径保证。

## 证据

- `before-close.png`：v3 浮动 QML 窗口显示在桌面 compositor 中。
- `touch-calibration-wifi-tab.png`：绝对坐标映射能命中应用标签。
- `relative-pointer-minimized.png`：相对指针命中同一窗口的最小化按钮。
- `touch-calibration-wifi-tab.raw`、`touch-calibration-after-wifi-tab.raw`：对应板端原始帧。
- `absolute-pointer-probe.py`、`relative-pointer-probe.py`：有界的 uinput 输入工具。
- `exit.json`、`processes.json`、`ready.json`、`service.log`、`ui.log`：板端生命周期原始文件。

各文件 SHA256 见 `receipt.json` 和 `SHA256SUMS`。本实验没有独立供电、人工看屏或人体触摸声明。
