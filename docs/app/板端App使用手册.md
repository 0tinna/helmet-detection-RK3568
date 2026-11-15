# 板端App使用手册

更新日期 2026年11月15日

本App让RK3568直接在板屏上启动安全帽检测，并在需要联网更新时手动连接WiFi。当前正式版本为v4-r2：日常检测使用板端模型和输入，可以离线运行，不需要板端浏览器、USB连接电脑或电脑页面。Qt Quick负责本地界面，Python服务只监听127.0.0.1，并使用每次启动生成的本机会话令牌保护控制请求。

当前使用已安装的Qt、Python、OpenCV、NumPy和RKNNLite，不训练或微调模型。v4-r2已在真实USB摄像头`/dev/video10`、720×1280全屏、检测中拔线TTL、回桌面常驻、双图标复用、UI退出和180秒连续检测上验收。systemui重启后的手指图标操作、断USB独立供电及真实WiFi认证/DHCP仍保留现场验收。

## 1 当前安装位置和入口

本手册对应当前v4-r2。正式安装根：

```text
/userdata/helmet-validation/20261115-native-app-v4-r2-local-2
  src/app/              本机服务 WiFi后端 Qt界面与启动器
  src/                  共用检测流水线与后处理
  models/               已有RKNN模型
  input/                回放演示输入
  results/              报警截图等本轮结果
  app-state/            私有会话目录 启动锁与日志
  vendor-entry/         厂商桌面启动文件和配置备份
```

点击“安全帽检测”进入检测页，点击“无线网络”或“Wi-Fi 设置”进入WiFi页。界面内也能在两个页签间切换。当前两个厂商入口和两个desktop已经指向v4-r2；`apk3.cfg`保持原SHA，`systemui`未重启。主界面下次重新加载后需人工确认图标仍显示并可手指点击，详见[图标安装与离线更新 v4](离线更新与图标安装-v4.md)。

历史入口v1/v2/v3仍保留，不覆盖旧根、旧包或旧结果。v4-r2迁移收据与备份位于`/userdata/helmet-validation/20261115-native-app-v4-r2-local-2/vendor-entry/icon-migration-r2-*`。回退前必须先正常退出新实例，再核对备份内容和入口目标。

当前启动器一次只允许一个resident会话。点“回到桌面”或关闭窗口只隐藏窗口，manager、service、Qt和检测继续运行；再次点任一图标复用同一session，只切换检测/WiFi页。点“退出应用”才请求完整清理。显示“仍在清理”时等待，不要为了重开强杀NPU或回滚网络的线程。

## 2 在屏幕上开始离线检测

1. 打开“安全帽检测”。等待本地服务和模型/输入列表出现。
2. 选择安装模型和输入。名称带“回放演示”的输入是已有图片或录像；“未验收设备”的摄像头节点只说明发现了设备，不说明能抓帧。
3. 选择回放配置。默认回放请求节奏10 FPS；响应测试为20 FPS请求节奏，均不是实测摄像头帧率或完整检测性能。
4. 点击“开始检测”。查看对应原帧画面、检测框、状态、原帧编号和独立检测FPS。
5. 点击“停止”，等停止完成后再切换模型或输入。App没有运行期模型热替换。
6. 点“回到桌面”可继续后台检测；再次打开会回到当前配置。需要结束进程时使用“退出应用”，并核对退出结果。

当前模型默认值来自已安装列表的第一项，本轮排序可能首先显示原640模型。它是可运行候选，尚未正式定型；不要把此前416性能结果归给默认原640。模型的`person`类别表示裸头，`hat`表示戴帽头部。现有App复用固定conf0.35、NMS0.45、skip0、TTL1秒的流水线，不把0.25诊断指标当作同一运行工作点。

状态说明：有新鲜对应结果才能显示检测画面。断流、过期、错误、停止或等待新结果时隐藏旧图，并提示不可用/等待。无检测表示当前有效帧未找到目标，不能推断全员戴帽。FPS显示为滑窗独立检测速率；完整会话、输出及显示速率仍按各自日志口径说明。

App通过本机回调显示画面，不需要Web浏览器。原Web远程输出作为另一功能保留，不能将宿主LAN预览当作当前Qt板屏触摸已验收。

预览继续使用双Image交替缓冲，换帧前不隐藏可见层；JPEG、frame ID、原采集时间、epoch/generation、selection和TTL从稳定包封装，失效源/过期仍拒绝。Phase 1实际DRM录屏300帧中预览空白帧0，运行中拔掉USB相机后约100 ms清空，低于1秒TTL。v4-r2当前`service.py` SHA为`d22000a1060a8c4a584661719b3825715a19b855267ab7772b376f3ad43e8c8f`，`resident.py` SHA为`ccd64e9043005b27d9174f4fea0f79ab5796a95dc511e5b910daed260540d23f`。屏上状态轮询帧号可能与正在显示的JPEG不同，不能用截图中单个状态帧号代替HTTP包身份验证。

## 3 保存与结果查阅

当前App调用共用Pipeline，报警截图保存到本轮根下`results/app-snapshots/`，对应sidecar记录原帧、模型和时间。只有有效新鲜证据会保存；没有文件应先查报警窗口、状态、路径及磁盘，不补写保存成功。

App当前没有屏上参数编辑、会话日志导出按钮、音量设置或事件录像开关。需要完整实验JSONL、资源sidecar、音频或MPP事件录像时，按[完整手册的CLI步骤](../项目蓝图与完整手册.md)另开隔离实验；不要将CLI功能全部描述为已在App界面暴露。

每次启动会在`app-state/session-.../`保留服务/界面日志、进程记录和退出结果。运行中的私有令牌及QML包装文件不是用户资料，不能公开复制；正常完整退出后启动器删除敏感会话文件。服务不记录带密码的HTTP请求或口令。

## 4 手动连接WiFi

v3将默认整体扫描等待由8秒改为20秒，单条控制命令仍最多2秒；显式调用者覆盖值仍按其设置。已观察真实扫描结果约13.87秒才到，旧v2因此超时。扫描中等待结果，不反复点击或强杀；超时/失败仍显示失败并保留原网络，结束后用户可检查模块/网络环境再手动重试。20秒有界等待不保证所有无线环境都能完成，不使用旧缓存假装新扫描成功。

联网只在用户主动点击“连接所选网络”时发生。检测本身不以联网为启动前提，进入WiFi页或扫描不会自动切换当前网络。

1. 打开Wi-Fi设置，查看当前SSID、连接状态和IP。
2. 点击“扫描附近 Wi-Fi”，等待附近网络名称、信号dBm与安全类型列表。
3. 点击支持的网络；也可输入隐藏网络名称。扫描列表里的中文SSID按UTF8显示，SSID上限为32字节，字符数不能替代字节数。
4. WPA2个人网络输入密码，开放网络选择“开放网络（无密码）”并保持密码为空。部分路由器同时广播旧WPA/TKIP与WPA2，只有明确提供WPA2/RSN的PSK加CCMP选项时才支持，连接仍强制RSN和CCMP，不回退旧算法。仅旧WPA/TKIP、WPA3、企业认证、WEP、OWE或未知认证不支持，不能按开放网络连接。
5. 点击SSID或密码框使用内置屏幕键盘。键盘支持字母大小写、数字、符号、空格和退格；默认密码遮掩，“显示密码”只供用户检查，避免让现场录像记录口令。键盘不承诺中文拼音输入；已扫描到的中文SSID可直接选择。
6. 如希望下次由wpa配置保留此网络，明确勾选“保存网络”；默认不勾选。
7. 点击连接，等待成功或失败状态。连接中不重复点击扫描或连接；成功须匹配新SSID、认证完成并获取可用地址。

密码在本机转换为PSK并通过私有控制套接字传送，不进入shell、进程参数、API响应或普通日志。勾选保存会请求wpa_supplicant保存其配置，可能持久化派生PSK；保存失败不能显示已保存。Python进程内的不可变字符串不构成保证擦除口令内存的机制，不把这一点写成已具备的功能。

手动连接会调用限定wlan接口的DHCP，其系统hook可能更新该网络所需的地址、路由和DNS，因此可能中断现有SSH或远程查看。这个动作由用户连接点击授权；自动验证只做控制状态/扫描与假接口测试，不实际切换当前WiFi，不杀wpa或操作无关网卡。

连接失败会删除本次新建网络条目，并尝试恢复原选择和每条既有网络启用状态。DHCP或恢复失败、没有可恢复旧地址基线时，应按界面失败信息检查原网络，不将“已恢复配置”解读为所有地址/DNS都已回到原状。不要因失败手动删除原有网络清单，先保留状态和日志。

## 5 日常启动和更新的区别

安装完成、兼容系统依赖已具备后，板端启动App和本地检测不需要电脑。U盘或电脑只用于首次复制、后续更新及排障；需要在线取文件时，在板屏先手动连接WiFi，再使用设备原有更新途径。

当前未配置检测应用开机后台自启动。板卡启动后，通过主界面图标手动打开是目标使用方式；“图标存在”与“断USB后能自行打开并检测”的人工验收不同。保留供电、显示、触摸、输入设备条件，USB连电脑供电与板端独立电源也要现场核对。

当前运行包为[v4-r2 ZIP](../../dist/helmet-native-app-20261115-v4-r2.zip)，39,227,461B，SHA `14b56543f7aca0b26c61b663770f8bf54729d0ba61a1ac5069f4f1143e084251`；外部清单为[bundle-manifest-v4-r2.json](../experiments/2026-11-15/native-app/bundle-manifest-v4-r2.json)。复制前核对外部SHA，再整体解压到新根并逐文件校验。v4-r2 ZIP含35 entries；五文件`code_identity`为`7cad142a4c5610dca2110fe7c22342c870317e322f28817ee1ccf9199b312302`。兼容Qt/Linux/RKNN仍须预装。

[v3包](../../dist/helmet-native-app-20261115-v3.zip)/[v2包](../../dist/helmet-native-app-20261115-v2.zip)/[v1包](../../dist/helmet-native-app-20261115.zip)及各自清单保留历史。旧包不覆盖或重新标为当前源码；更早`helmet-offline-20261114-final.zip`是CLI载荷，不含App。

## 6 目前证据与尚待人工验收

当前[v4-r2 Phase 2终验](../experiments/2026-11-15/native-app-usability-v4-phase2-1/report.md)记录正式图标启动、隐藏30秒、复开、双图标切页、UI退出、180秒连续检测和图标迁移。隐藏期间31个状态样本帧号`989→1942`，QML frame request/decodes/presentations冻结；180秒采样实际196.67秒，帧号`527→6312`，零倒退、零invalid、三进程零变化。随后UI停止并退出，service exit0、lease released、NPU 0%。

最新[WiFi v3安装记录](../experiments/2026-11-15/native-app/wifi-v3-install.json)绑定wifi SHA `648b714137b18538f8f8e6a355565b07aec8fb01270c3deed5c01eba30249468`，旧v2 WiFi源码备份保留。[v3实际厂商wrapper/normal launch.py验证](../experiments/2026-11-15/native-app/20261115-native-launch-checks/v3-wrapper-wifi-1/manifest.json)30秒退出0、service/UI exit0/private clean，当前网络仍COMPLETED且未连接新网络；[实际Qt内容图](../experiments/2026-11-15/native-app/board-sessions/session-frbzytg2/screen.png)可见至少3个SSID选择按钮和屏幕键盘。它补扫描列表/键盘真实自动渲染，不证明人工点击、认证/DHCP/保存或新的NPU长跑。

默认等待14秒合法扫描的行为回归与WiFi自有35项=33pass/2Linux平台skip、独立复审见[修复复审](../../.superpowers/sdd/2026-11-14-graduation-scope/native-wifi-scan-timeout-review.md)。唯一生产变动为scan default8→20，command2/connect20/rollback5、解析、选择与取消规则保持；显式override不是新增独立短超时硬件测试。旧v2页失败/空列表和13.87秒迟到事件仍保留，不能改成从未失败。

最新[native-smoke-4验证](../experiments/2026-11-15/native-smoke-4/run-verification.json)使用v2服务、既有416、construction-replay.avi、responsive请求20、`.35/.45`/TTL1和实际Qt/本机服务。12秒会话134真检测/133输出，11.123010861检测/11.040003318输出FPS，3次active HTTP预览200可解码，native_gui_verified=true，直接SDK/source/worker及HTTP/App/GUI清理accepted。仅本短会话固定10FPS达到，`thirty_minute_app_passed=false`；无物理摄像头/人眼/WiFi切换。验证用私有QML wrapper直接调用DetectionController/LocalService，并未执行正常launch.py，不冒充厂商图标v2启动验证。

[新Qt内容截图](../experiments/2026-11-15/native-smoke-4/screen.png)可见416回放/框与选择项，实际显示FPS仍未测，采样可见性不证明连续全程可见或触摸体验。原[native-smoke-2](../experiments/2026-11-15/native-smoke-2/run-verification.json)缺有效GUI图与[native-smoke-3](../experiments/2026-11-15/native-smoke-3/run-verification.json)healthy间HTTP503均保留failed，不由新结果改写。

新增服务23项=22pass/1symlink权限skip；[主树](../experiments/2026-11-15/native-app/local-main-after-preview-fix.log)250项=245pass/5skip，[legacy](../experiments/2026-11-15/native-app/local-legacy-after-preview-fix.log)25项=24pass/1NPU skip，软件范围见阶段总结。[native-longrun-1终态](../experiments/2026-11-15/原生App30分钟验证报告.md)已completed/exit0：实际1800.051066秒，19,806检测/19,805输出、11.0030283/11.0024728FPS，360次active HTTP可解码，直接SDK/source/worker/service/GUI清理accepted，30分钟与固定10FPS通过，仅限既有416/单照片录像/响应20供给/完整日志和30秒截图的验证组合。

运行采用真实DetectionController/LocalService/Pipeline/RKNN及QML私有验证wrapper，未执行正常launch.py/厂商图标路径，非物理摄像头。running阶段GUI采样3,443/3,601=95.6123%ready/visible，是观察比例，不能称连续显示或实际显示FPS。capture→sink completion p95/p99为244.6833/323.6640ms，不含Qt/物理呈现。+60秒后Python RSS增3.406MiB/OLS+.13012MiB/min；Qt末点差−1.551MiB但OLS+.45562，末尾抓图附近下降不掩盖正趋势，未设无泄漏阈值。ondemand未锁频，实际触摸/供电/WiFi、独立85%/最终配置仍待验。

此前v1协调者记录的Qt短验证包括检测/WiFi页各8秒exit0。[检测页截图](../experiments/2026-11-15/native-app/detection-screen.png)、[含键盘WiFi图](../experiments/2026-11-15/native-app/wifi-keyboard-screen.png)及[旧最终样式键盘](../experiments/2026-11-15/native-app/wifi-keyboard-screen-final.png)是screenRoot内容，不包括顶部header或物理屏幕照片。截图6.40是原640候选滑窗FPS，不属于新416完整性能。自动demo属于验证动作，不是用户启动后自动检测策略；这些v1证据保持原源码身份。

只读无线状态与扫描资料包括[原扫描记录](../experiments/2026-11-15/native-app/wifi-readonly.json)、[原始扫描flags](../experiments/2026-11-15/native-app/wifi-scan-raw.txt)和[新解析器只读扫描记录](../experiments/2026-11-15/native-app/wifi-readonly-current.json)。早期解析器将混合旧WPA/WPA2广告全部标unsupported，该结果保留为失败诊断；新记录正确解析15个有RSN/CCMP选项的WPA2个人网络，status仍为原网络COMPLETED、IP192.168.1.3，`actual_connect_attempted=false`。新解析器不允许legacyfallback，扫描成功不证明实际连接或DHCP切换。Linux假控制与进程组测试日志已保留，实际网络切换没有自动执行。

[本机服务复审](../../.superpowers/sdd/2026-11-14-graduation-scope/native-service-review.md)核对了令牌、路径、原帧时效与停止所有权；[WiFi实现报告](../../.superpowers/sdd/2026-11-14-graduation-scope/native-wifi-report.md)保留假接口、Linux权限和进程组验证边界。服务后续generation/epoch绑定更新及实际界面状态以最终复审和终态清单为准，不把较早报告转记为所有后续修复已实测。

仍需现场完成：systemui重新加载后人工看到并手指点击两个图标、断USB后独立供电/启动、真实WiFi关联/DHCP/保存及失败恢复、长期现场、报警可听性，以及正式场景质量和最终配置。物理USB相机采集、720×1280全屏、拔线和180秒短跑已通过；这些不替代长期现场。已有回放长跑低于10 FPS、诊断mAP低于85%且独立性未知的限制保持。

## 7 常见问题

**主界面没有新图标。** 厂商`apk3.cfg`在主界面启动时读取；v4-r2入口和desktop已迁移，但本轮没有重启`systemui`。用户可在方便时通过`sh /etc/init.d/S50systemui restart`人工重载，再检查两枚图标；执行前先确认当前没有未保存操作。

**提示App已运行。** 当前会话可能仍有窗口、NPU或WiFi回滚。等待正常关闭；查看本轮退出记录，而非强杀后覆盖锁。

**开始后无图。** 核对当前输入是回放还是未验收节点、模型存在、状态和TTL。摄像头节点不保证有图；首次现场接入按[摄像头验收手册](../field-validation/摄像头接入与现场验收.md)进行。

**扫描失败或无线状态未知。** 保存当前错误状态，检查无线模块和wpa控制路径是否存在。不能调用厂商带明文密码argv或killall脚本替代App后端。

**关闭窗口后迟迟不结束。** 正在等待设备worker或网络事务结束时，启动器保持资源所有权和锁；当前设计不承诺native挂死、断电或外部SIGKILL一定清理。保留日志再排障。

**版本或包不确定。** 以实际SHA、部署manifest、源码和终态记录为准。旧包不含App，HTML开题演示也不是本地Qt应用。

v1/v2/v3终验、禁网、图标和30分钟记录均按原SHA保留，不回填为v4-r2。v4-r2的Phase 1/2报告、正式链接收据和180秒证据见[Phase 2终验](../experiments/2026-11-15/native-app-usability-v4-phase2-1/report.md)。触摸自动化驱动实际Qt控件，但不是人工手指；独立供电、真实联网与长期质量仍待验。
