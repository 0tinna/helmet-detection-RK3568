# 原生App安装与独立运行验证 · 2026-11-15

v2原生包已校验并完成入口迁移；实际厂商包装器调用正常launch.py完成真实NPU/Qt短回放，独立网络命名空间内只有lo UP时也正常退出。以上是安装、正常启动路径与本地断网依赖证据，不能替代人工图标点击、触摸/独立供电、摄像头或416组合30分钟性能。

旧v2 WiFi页失败已确认默认8秒预算小于真实13.8725秒扫描耗时。WiFi-only v3将默认整体等待改20秒，在原目录/服务/模型的实际normal wrapper界面完成30秒列表/键盘验证：至少3SSID清楚、当前COMPLETED、exit0/private clean；未真正连接。v3只是WiFi修订，旧v2 416长跑与失败保持原身份。

## 1. 发布与迁移身份

[v2ZIP](../../../dist/helmet-native-app-20261115-v2.zip)/[实际清单](native-app/bundle-manifest-v2.json)为39,195,268B/28payload、SHA `f8957945cb90084f8c32cff789b07e0e4ca1c08690c493c5ed3593eacb4a4989`。包内两份App手册哈希固定，后续实际安装/迁移/短验证记录在包外维护，不因外部状态变化重打包。v1ZIP22585661…及其历史图标/禁网记录保留。

[迁移result](native-app/migration-20261114T222113212364Z/result.json)为applied=true，备份目录`/userdata/helmet-validation/20261115-native-app-v2/vendor-entry/migration-20261114T222113212364Z`；旧链接及两个desktop原内容/修改后记录留档。实际四个已拥有链接从v1改到v2：

| 入口 | 新目标 |
| --- | --- |
| /opt/ui/src/apps/helmet_app | /userdata/helmet-validation/20261115-native-app-v2/vendor-entry/helmet_app |
| /opt/ui/src/apps/helmet_wifi | /userdata/helmet-validation/20261115-native-app-v2/vendor-entry/helmet_wifi |
| /opt/ui/src/appicons/helmet_app.svg | /userdata/helmet-validation/20261115-native-app-v2/src/app/icons/helmet.svg |
| /opt/ui/src/appicons/helmet_wifi.svg | /userdata/helmet-validation/20261115-native-app-v2/src/app/icons/wifi.svg |

两个desktop Exec同步v2根；厂商apk3.cfg前后SHA均`f64a8e2f751ff777b98a36a13a0ad062e0812f2bd6ebd7914a512b772f507b11`，没有为迁移改变清单。`desktop_restarted=false`、`human_icon_tap=false`、迁移本身`detection_started=false`/`wifi_changed=false`。链接迁移和自动包装器测试不证明用户已看到/点击桌面图标，也不证明WiFi连接。旧root和数据/备份保留以供明确回退。

## 2. 实际包装器与正常启动器短验证

[v2-wrapper-detection-1 manifest](native-app/20261115-native-launch-checks/v2-wrapper-detection-1/manifest.json)实际运行`/opt/ui/src/apps/helmet_app --auto-demo --smoke-seconds 12`，wrapper target为v2，`launch_py_executed=true`，service/UI退出0、cleanup_completed=true/private_material_cleaned=true、host namespace preserved。

[对应Qt内容图](native-app/board-sessions/session-qxovl138/screen.png)实际可见默认原640、1.jpg、replay10和当时滑窗6.20FPS。该12秒短验证支持真实normal launch.py/service/NPU/QML路径，不代表416、construction-replay/20供给/日志截图的11.0030FPS长跑，也不是完整会话吞吐或物理屏幕照片。验证专用auto-demo不是日常自动检测策略；用户正常使用从App开始/停止。

## 3. v2包装器lo UP独立命名空间

[v2-wrapper-offline-2 manifest](native-app/20261115-native-launch-checks/v2-wrapper-offline-2/manifest.json)与[network记录](native-app/20261115-native-launch-checks/v2-wrapper-offline-2/network.json)确认child CLONE_NEWNET为独立net namespace、仅lo UP，没有其他网络接口。实际包装器进入正常launch.py并完成真实NPU/Qt回放，service/UI exit0、cleanup_completed/private_material_cleaned=true、host_namespace_preserved=true。父主机/板端网络未被切断或重设。

[离线Qt内容图](native-app/board-sessions/session-q3v0cgi7/screen.png)可见原640/1.jpg/replay10及滑窗6.30FPS；短内容渲染不是实际显示FPS验收。应用在预装兼容Qt/Linux/Python/OpenCV/NumPy/Lite2/驱动后本地启动/检测不依赖运行外网；此证明不是空白板OS离线安装，也不是断USB独立供电或物理WiFi拔除。

前一offline-1尝试因板端没有unshare依赖而未启动App，空输出目录作为实验工具依赖失败保留，不能记为成功空推理或App故障修复。offline-2另用child CLONE_NEWNET完成真实路径，不覆盖第一次失败。

## 4. WiFi UI失败与后端成功的矛盾

[wifi-1](native-app/20261115-native-launch-checks/v2-wrapper-wifi-1/manifest.json)实际v2 wrapper/launch.py运行，第一次截图在扫描进行中，不能算完成；[wifi-2](native-app/20261115-native-launch-checks/v2-wrapper-wifi-2/manifest.json)/[wifi-3](native-app/20261115-native-launch-checks/v2-wrapper-wifi-3/manifest.json)在较长20秒观察中退出0，但[第二次内容图](native-app/board-sessions/session-4za4a64s/screen.png)显示“操作失败，请检查无线模块和网络设置”，第三次也失败/列表空。服务/UI正常退出仅证明生命周期，扫描业务操作未成功，不能把exit0当WiFi功能通过。

[后端直接扫描诊断](native-app/wifi-scan-diagnosis-v2-1.json)使用真实wpa控制，SCAN事件/SCAN_RESULTS返回16个网络，error=null；[WifiActions诊断](native-app/wifi-action-diagnosis-v2-1.json)action kind=scan/status=completed，result含16网络/error=null。两次status before/after均原COMPLETED及原IP，connect_attempted=false，没有SELECT_NETWORK/认证/DHCP/保存实验。

后续[正常UI协议trace](native-app/wifi-ui-trace-v2-1.jsonl)记录真实ATTACH/SCAN OK及约8秒event timeout；[迟到事件诊断](native-app/wifi-late-event-v2-1.json)production_elapsed8.035351601秒，独立raw monitor在13.872536秒收到实际扫描结果事件，随后SCAN_RESULTS成功。根因为v2默认8秒扫描等待短于本次真实扫描耗时，不再笼统写未知UI问题。协调者已将WiFi-only默认20秒及14秒迟到行为修复交实现验证，service/QML/Pipeline不变；后续v3实际列表/键盘与lo UP短run已补，包身份由实际清单维护。旧wifi-2/3失败、旧15网络/v1资料仍原样保留。

### 4.1 WiFi-only v3实际修复与界面复验

[v3安装](native-app/wifi-v3-install.json)仅wifi.py default scan8→20秒，SHA `648b714137b18538f8f8e6a355565b07aec8fb01270c3deed5c01eba30249468`；旧WiFi `1e998474293b5499e0cedf5fa57317416ac49dab7a2b9f36fd93b97ec30c20fb`备份原样。目录仍名`/userdata/helmet-validation/20261115-native-app-v2`，service9a2f7454…、Pipeline/QML未变，v2长跑不追认为v3新组合。

[v3 actual wrapper manifest](native-app/20261115-native-launch-checks/v3-wrapper-wifi-1/manifest.json)厂商helmet_wifi→normal launch.py、30秒scan/keyboard验证，service/UI exit0/private clean、父namespace保持、governor前后ondemand。实际[Qt内容图](native-app/board-sessions/session-frbzytg2/screen.png)至少3个SSID选择按钮、键盘与原网络COMPLETED/IP清楚。它补真实UI扫描列表，未发生connect/认证/DHCP/保存、人工点击/触摸或NPU长跑。

[35项软件行为/独立复审](../../../.superpowers/sdd/2026-11-14-graduation-scope/native-wifi-scan-timeout-review.md)33pass/2Linux skip；默认14秒合法扫描以fake时钟走实际解析/命令序列/选择不变，不只检查常量。overall默认20秒、command2秒与显式override保持，connect20/rollback5/FAILED拒绝/取消/清理不变。20秒不是所有无线环境成功保证；失败结束后用户检查环境再手动重试。旧wifi-2/3失败与8.035s/13.8725s原记录保留。

[v3同源码离线验证](native-app/20261115-native-launch-checks/v3-wrapper-offline-1/manifest.json)/[network](native-app/20261115-native-launch-checks/v3-wrapper-offline-1/network.json)run_id v3/wifi648b7141…，真实normal wrapper→launch.py/NPU/Qt exit0/private clean，child CLONE_NEWNET仅lo UP、父namespace保留；[画面](native-app/board-sessions/session-8x9pmyxl/screen.png)已归档。kind/目录仍名v2是工具历史命名，不修改原raw，不推为旧revision。另[Linux WiFi35/35](native-app/wifi-v3-linux-tests.log)协议fixture全pass，主机35项33pass/2skip；真实连接仍未测。

[v3包](../../../dist/helmet-native-app-20261115-v3.zip)/[实际清单](native-app/bundle-manifest-v3.json)已一次生成并冻结：39,196,872B、28项载荷/6模型，SHA `a6ddec8f0d5579a62df261fc84a8f92cbce7b27c54ead79e9579f6e87e11058d`。逐文件与当前源码一致，相较v2仅wifi.py和两份App手册变化，旧v2封包f8957945…未覆盖。

[板端发布预检](native-app/v3-release-board-precheck.json)确认App进程已退出、NPU load0%，仅两份文档仍为v2字节；[实际安装收据](native-app/v3-installed-release.json)记录正常停机检查后只更新这两份手册，旧文档原样备份在vendor-entry/docs-before-v3。全部28项已与v3清单逐文件匹配，四图标链接/cfg仍不变，检测源码/模型/QML/网络和桌面未在这次操作中改变。板端安装根继续历史v2名称；这次文件核验不新增v3完整组合30分钟证据。

## 5. 与长跑、论文材料及现场接受条件的关系

[原生App30分钟报告](原生App30分钟验证报告.md)只通过controller/service+QML私有验证路径、既有416/单照片20供给/完整日志/30秒截图。此次normal launch.py/实际包装器及lo UP验证补正常启动/运行时外网依赖的短路径证据，没有为normal launcher做30分钟，也没有关闭连续显示/内存无泄漏或最终模型配置门槛。

两App手册在v1/v2/v3各封包内保持其原字节，实际入口/发布身份以本安装记录和外部清单为准。可编辑PPTX草稿以[导航](../../deliverables/README.md)最新验证版为准；学校模板/个人信息需填，PPT图片/备注完整不等于物理演示验收。

仍需人工或独立资料：完整板屏/触摸/滚动/键盘体验、断USB后独立供电/图标操作、真实WiFi关联/DHCP/保存/失败恢复、摄像头型号/格式/曝光/采集/拔插、声音/手机、独立85%/S1来源场景和S6联合最终配置。WiFi-only v3实际列表/键盘自动渲染已补，真实连接/人工与v3完整组合长期仍需各自证据，不以列表成功替代关联/DHCP/保存。
