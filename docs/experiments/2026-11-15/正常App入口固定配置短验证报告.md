# 正常App入口固定配置短验证报告

2026年11月15日，本轮验证已完成的v3正常厂商入口、真实页面配置和NPU回放。它补充此前默认640/单图短测；新后台常驻v4仍需独立验收。

真实路径为`/opt/ui/src/apps/helmet_app → launch.py → service.py → Main.qml`，安装根保留历史名`/userdata/helmet-validation/20261115-native-app-v2`。临时uinput键盘先经无NPU的[probe6](normal-launcher/normal-ui-probe-6/verification.json)确认页面选择，再从正常新窗口选择416/录像/responsive并按开始。验证器没有调用API start、写WiFi或替换QML/Pipeline/SDK。

| 条件或结果 | 本轮证据 |
| --- | --- |
| 模型/来源/profile | helmet_416x416.rknn / construction-replay.avi / responsive |
| 工作点 | conf.35、NMS.45、skip0、TTL1；请求回放供给20FPS |
| 检测观测时长下界/上界 | 50.013266919 / 50.078472456秒 |
| 边界计数差 | 检测557、输出557、捕获959 |
| 保守检测/输出FPS | 均11.122543734；计数差除以时长上界 |
| 正式采样 | 51次status、9次preview；9张JPEG均640×480且可解码 |
| 正式最大采样间隔 | status约1.0258秒、preview约6.0248秒；正式资源约1.0318秒 |
| 退出 | 空POST stop后running=false；wrapper/service/Qt均exit0、绑定PID/start token终态 |
| 清理 | 私有token/App.qml及临时uinput消失，governor前后rknpu_ondemand |
| 30分钟门槛 | 未满足；这是固定配置短观测 |

命令的`--duration 12`是最低请求区间，验证器为导航、原生截图和退出保留额外时间，并等待App原有截图计时器。本轮实际区间约50秒，不能写成“实际只运行12秒”或用请求时长作FPS分母。计数是两次完整状态之间的差，不是全部帧库存，也不证明所有pending结果已输出。

[宿主重算](normal-launcher/normal-v3-short-1-host-verification.json)为passed，固定10检测/输出FPS通过，30分钟组合门槛为false。原目录有65个HTTP响应，均200：54status、9preview、1config、1stop。原始22文件由20项必须哈希库存和manifest/派生verification两项排除构成。24项实际记录的源码/图标/模型身份与[冻结v3清单](native-app/bundle-manifest-v3.json)相符；本轮未重新逐文件检查其余4项载荷，全28项安装证据仍以较早[安装收据](native-app/v3-installed-release.json)为准。

[原生截图](normal-launcher/normal-v3-short-1/screen.png)实际显示416/录像/responsive与标注画面。APP_RENDER的状态帧号957与画面注释962不同，故仅支持新鲜原生画面和配置的观察，不能当作精确呈现帧元数据绑定。screenRoot截图也不包括顶部header或物理屏幕。新v4验证设计已要求抓取实际Image.Ready包、异步保存完成与有效TTL的关联。

启动/导航阶段资源采样有8.593秒间隔，保留在原日志；正式区间内的资源采样另计，不能将整场称为全程每秒采样。默认有界遥测与附加HTTP/proc观察负载区别于旧v2显式完整日志/30秒截图长跑，FPS差值不归因为单因素优化。

[独立证据审查](../../../.superpowers/sdd/2026-11-14-graduation-scope/normal-v3-short-evidence-review.md)裁定短跑范围PASS。失败的probe4保留：其窗口已正常退出，但冷解码与原生两秒尾窗使观察器未确认stop。本轮启动前准备解码依赖、校准安全按键后取得新成功证据；旧失败没有改为成功。

本轮是单照片生成录像、原正常窗口生命周期；不证明后台常驻、真实摄像头、物理点击/触摸、独立供电、连续显示、独立SDK release收据、无泄漏或85%精度。后续长跑针对[新后台App](../../superpowers/plans/2026-11-15-resident-board-app.md)的当前入口和隐藏区间重新建立证据。
