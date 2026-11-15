# 原生 App 正常启动路径验证设计

本轮继续用户已授权的毕业计划和多 agent 自主实现。上一轮完成 v3 发布与资料交付；本轮补正常厂商入口 `helmet_app → launch.py → service.py → Main.qml` 的长期运行证据。物理摄像头与人工点击/触摸/独立供电继续延期。

## 依据与方法选择

既有 v2 App 30 分钟试验直接构造 DetectionController/LocalService，并加载私有验证包装器；它没有运行正常 launch.py。当前 v3 正常启动仅有默认原 640 模型、1.jpg、replay10 的短证据。正常服务使用有界遥测，不开启完整逐帧日志或每 30 秒定时截图。

选择真实厂商包装脚本，保持已冻结的 v3 源码、模型、QML、发布包与图标目标不变。用临时 Linux uinput 键盘操作现有检测页，通过 App 自带的验证截图和实际 API 返回确认操作结果。对照路线是外部 API 直接启动；它会绕过页面选择，可能让下拉框与实际模型不一致，因此不用于本次配置与开始操作。修改生产验证参数或替换生成的 App.qml 会改变受测路径，本轮不采用。

只读可行性观察已确认 Wayland 720×1280 输出、/dev/uinput 与 qmlscene 可用，NPU 当前空闲、governor 为 rknpu_ondemand。weston-screenshooter 返回“unauthorized”，虽退出 0 但没有图像，不能把它当截图成功，也不调整 compositor 权限。先做不启动 NPU 的有界按键/截图探查，再决定具体检测页按键序列。

## 固定配置与边界

- 安装根固定 `/userdata/helmet-validation/20261115-native-app-v2`，包身份为 20261115-v3，目录历史名不变。
- 厂商入口固定 `/opt/ui/src/apps/helmet_app`。真实 launch.py 创建私有会话、正常服务与 QML；不修改或替换其 App.qml，不用 LD_PRELOAD、sitecustomize、定制 Pipeline factory 或替代 SDK。
- 正式候选模型 `helmet_416x416.rknn`，来源 `construction-replay.avi`，profile `responsive`。源码固有 conf.35/NMS.45/skip0/TTL1 与请求供给20保持，来源仍为单照片生成录像，非摄像头/独立场景。
- 先 probe 检测页；probe 仅发送 Tab/Home/Up/Down/Space/Escape 等导航键，不按开始按钮，不切 WiFi，不打开终端/系统设置。任何实际检测启动必须属于单独正式 run。
- 正式 run 的选择和开始必须经真实界面按键。API仅 GET配置/状态/预览，以及结束或故障时 POST空对象至 /api/stop；禁止 /api/start 和任何 WiFi写操作。启动后实际selection必须与固定配置一致，否则立即正常stop并记录失败。
- 输入设备具有本任务唯一名称；创建仅增加临时键盘，关闭FD/销毁后应消失。不更改物理输入设备、权限、udev、桌面配置或网络设置。只向本次已确认存活的 App 会话发送有限白名单键；Alt+F4仅用于正常关闭本次窗口。
- 所有新证据和测试辅助文件在工作区或 `/userdata/helmet-validation` 新唯一目录。已有目录、模型、ZIP、长跑证据和 App 两份封包手册不覆盖。敏感token只在私有会话读取和HTTP请求头使用，不进日志/argv/环境/清单；不复制token或App.qml。
- 只运行一个 NPU会话。开始前、异常退出后核验真实PID/start token、锁与负载；无法确认终态时观察同一进程，不重启或外杀 SDK线程/服务。

## 观测和结论

开始后确认真实 selection、running与至少一个新鲜预览，再建立两次状态采样之间的观测区间。正式区间的保守下界必须至少1800秒；检测计数和输出计数分别由边界状态的计数差计算。时长与计数读取有HTTP往返区间，报告其上下界，用较大的时长作保守FPS分母，不借用旧run计数或默认滑窗FPS。

每秒记录API状态与独立 `/proc` 进程资源，每5秒保存实际JPEG/原响应/帧号/采集时间/epoch/generation/TTL。响应失败、503、缺口、计数下降、身份改变、源/推理失败均保留。计数与采样证据不扩称完整逐帧库存、精确全程分位或持续GUI可见。App末期自带截图应解码并能确认实际模型、来源、profile和画面；它仍只覆盖screenRoot、不含整个物理屏幕。

资源按launcher/service/Qt/observer分别记录RSS、CPU、线程与FD、温度及NPU频率；只作描述，预设60秒及最后300秒等窗口，并保留原时序。没有约定的泄漏阈值，不以末点下降或新试验结果制定“无泄漏通过”。此次正常有界遥测负载与旧完整日志负载不同，不能把FPS差值写成单因素优化收益。

结束先正常stop并确认工作线程停止，再等App原生退出计时或对本次窗口发送Alt+F4；不得kill服务/SDK所有者。核验launcher/service/Qt终态、exit.json、token/App.qml清理、临时输入设备消失、governor前后一致。退出收据证明正常生命周期，不能冒称独立直接SDK release收据。

本轮可提升的条件是“当前发布正常启动路径、有界遥测、固定回放配置的长期观测/正常退出”；物理摄像头、人工触摸/电源、实际WiFi认证/DHCP/保存、独立85%、最终联合配置和持续物理显示仍保持开放。
