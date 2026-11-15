# 原生 App 内网查看与开机入口验证（v5-r3）

日期：2026-11-15。基线提交：`379ce33d8fb0c11b1e343ecf7789b3fb08c1e612`。本轮按用户新增的“开机自启动/启动 App 能通过手机内网访问”要求实施，全部由主 agent 完成。

本轮已配置 `http://192.168.1.3:8091/` 的只读页面和 BusyBox init 入口，验证同一检测实例、USB 相机、隐藏/复开、双入口及退出。最后复核观察到实际重启及其后的 App 使用/正常退出；**无人提前打开 App 的纯自动后台开机过程、实体手机仍未单独验收，手机持续无空档采样未通过。** 未验项不以其他 run 补写。

## 1. 最终运行包与安装

| 项目 | 记录 |
| --- | --- |
| 运行包 | [helmet-native-app-20261115-v5-r3.zip](../../../../dist/helmet-native-app-20261115-v5-r3.zip) |
| 字节/entries | 39,229,911 / 40（39 payload + 内部 manifest） |
| ZIP SHA256 | `ce7dffe010f5da5828dfce8586bc6cf799dbd43b9ed3dcdbe71eb2042d030472` |
| 外部清单 | [bundle-manifest-v5-r3.json](bundle-manifest-v5-r3.json) |
| 五文件 identity | `1e10f7e9b5712e330339b366d0495eb72e8f0210139f5f1e163a9459b8f2b9f8` |
| 完整新根 | `/userdata/helmet-validation/20261115-native-app-v5-r3-lan-3` |
| 实验目录 | `/userdata/helmet-validation/20261115-app-lan-validation-3` |
| 版本 tag | `native-app-v5-r3`，标记本轮最终源码、资料和运行包 |

整包复制、解压，逐项核验字节数、SHA、执行权限与五文件身份，再编译/help/sh 语法检查。[部署收据](board-v5-r3/deployment.json)保留实际结果。没有向已归档根逐个拼装源码；本轮各部署根的载荷均与对应 ZIP 匹配。历史 v2 验证副本的临时 SHA 偏差仍只属于[旧 Phase 1 报告](../native-app-usability-v4-phase1-1/report.md)，旧记录保持原样。

内部 manifest 的 `physical_camera_tested=false` 等字段是**封包时**状态。封包后的实验另以本报告及收据绑定同一 ZIP，未回写冻结 manifest。

## 2. 实现边界

`lan.py` 消费现有 `DetectionController.preview_package()`；Qt 与 LAN 共用原 Pipeline、摄像头、NPU 和已校验的 JPEG。新页面使用 `/api/preview`，独立计算剩余 TTL、扣除请求耗时，迟到解码/挂起请求不能恢复过期图。`/status.json`、`/frame.jpg`、`/stream.mjpg` 也是只读接口；外部客户端应自行遵守帧身份和 TTL，原始 MJPEG 客户端的显示保持行为不属于网页保证。

App 控制保持随机 `127.0.0.1` 端口与私有令牌；`0.0.0.0:8091` 不提供开始、停止、WiFi 或退出。开机入口只启动后台待机和监听，不自动开始检测；图标首次显示 Qt，之后复用实例。完整退出关闭两路 HTTP、设备线程、Qt 和锁；异常/缺失退出收据不得报告成功。

保留独立 POSIX session、Unix 控制、五文件 identity 和原有 owner/lease 校验。QML、模型、Pipeline、后处理、源模块、报警与输出模块没有变化。`person=裸头`、`hat=戴帽头部`；conf `.35`、NMS `.45`、skip `0`、TTL `1s`。

## 3. 原因、失败与修复

| 记录 | 实际结论 |
| --- | --- |
| 首个 v5 后台 | session `e4fe02d746e14bdaa24f35f86f87660d` 在待机约 145 秒后 supervision 失败，服务 exit 0、清理及 lease 释放为 true；失败 phase 保留，180 秒相机脚本尚未开始。[原收据](board-v5-first/background-failure-1.json) |
| 独立 Linux 文件诊断 | 91 次写/657 次读后复现 `stat` 返回刚被替换的旧 inode，`nlink=0`，原读取误报 singly-linked 不安全。[堆栈与元数据](board-v5-first/record-io-diagnostic-1.json) |
| 竞态回归 | [RED](board-v5-first/kernel-record-red2.log)新增 2 项失败；修复只重开权限/owner/类型均可信且已解除目录链接的记录，不读取旧 inode，不重试不安全元数据。最终 [10 项真实 Linux 回归](board-v5-r3/kernel-record-green3.log)全部通过 |
| 修复后的并发诊断 | 25.003 秒，5,762 写、33,454 读、599 次替换观察、0 errors。[原记录](board-v5-r2/record-io-diagnostic-2.json)；属于 r2 诊断，不充当 r3 相机成绩 |
| 旧 v4-r2 临时再启动 | session `5c054835dd844b6abc22f48da53184c8` 也以 supervision failed 结束；迁移先确认其所有进程和锁已释放，没有将 failed 写为 completed。[交接记录](board-v5-r2/handover-terminal.json) |
| 重复 stop 复核 | 无进程时直接返回 absent，会掩盖同根旧会话的 failed/缺失收据。新增 [RED 3 项](host/boot-absent-red.log)，修复后 [10 项 GREEN](host/boot-absent-green.log)。r3 真实重复 stop 还验证同 terminal session |

首个 v5 会话原日志没有堆栈，因此其确切异常没有直接留存；独立诊断确定存在上述竞争，不伪造该会话的异常文本。最早 kernel fixture 还记录了 socket 路径过长和 symlink 导入路径不一致，后续以短隔离路径及测试内规范化修正，未改变生产权限校验。

首个 v5 ZIP、v5-r2 ZIP、manifest、根和所有失败日志均保留。r2 相机与待机成绩见各自的 `board-v5-r2/`，最终 r3 另做实测。

## 4. 最终 r3 USB 相机与常驻

[camera-summary.json](board-v5-r3/camera-summary.json)及本机保留的 `camera-180s.jsonl` 记录同一次运行。通过正式 `/opt/ui/src/apps/helmet_app`、`helmet_wifi` 入口调用；开始/停止采用本机认证 API。没有手指触摸操作。

| 项目 | 实测 |
| --- | --- |
| session | `881496eecc3b4cbfbb797c049017b9dc`，首次检测页、WiFi 页、再次检测页均相同 |
| manager / service / Qt PID | `16762 / 16764 / 16839`，starttime `1482780 / 1482858 / 1483657`，全程不变 |
| POSIX session id | `16762`，等于独立 manager PID |
| 输入 | `/dev/video10`，MJPG 640×480，驱动报告 30 fps；`/dev/video11` metadata 不使用 |
| 模型 | `helmet_416x416.rknn`，SHA `7e0eefe4bfe86627bb6b58161cacca10912e0dbe1ba2513f58d091c40b26cd48` |
| 配置 | responsive / conf .35 / NMS .45 / skip 0 / TTL 1s |
| 时长/样本 | `180.502223025 s / 138` |
| 预览帧号 | `127→4727`，0 倒退、0 invalid 样本、0 not-running、0 PID 变化 |
| 隐藏期间 | 28 样本，`935→1892`，增加 **957** |
| 隐藏预览计数 | requests `151`、decodes `133`、presentations `132`，各样本保持不变；状态轮询继续 |
| 重复 init start | 90.4095 秒时仍同 session，窗口 intent 不变、检测未重启 |
| 停止检测 | `stopped=true`；公共 preview 返回 503、valid false、frame_id null、alarm unknown |
| 退出/重复退出 | 两次均 `phase=completed`、session 同上；确认 owner 已结束与 kernel locks 释放，NPU load 0% |

首个 LAN JPEG 真实解码为 `[480,640,3]`，其 SHA 为 `e0953c46ead6e0aa83be51c9ac697511dc7718bc657f50778a7fffd40e4bfc0c`。generation `7956f38f9cab489c8a3d1bc1835ddf50`、source epoch `0`、frame `87` 和采集时间 `14838.625960426` 保留在同一包中。

首末采样点相隔 179.051684 秒，captured `134→4735`、detected `47→1532`、output `42→1263`；分别约 `25.70 / 8.29 / 6.82 FPS`，不是驱动请求值。最后窗口的独立检测/输出为 `8.4 / 6.6 FPS`。该窗口推理 p50/p95/p99 为 `62.84/75.84/80.77ms`；capture-to-output 为 `225.37/341.15/375.08ms`，capture-to-sink-complete 为 `349.81/457.30/493.50ms`。这些是末窗口分位数，未宣称全程尾延迟或实际屏幕 FPS。

板端 Python 3.10.5、Linux 5.10.160/aarch64、OpenCV 4.5.4、NumPy 1.21.2、RKNN-Toolkit-Lite2 2.5.0，governor `schedutil`。本轮没有修改 governor。[运行环境](board-v5-r3/runtime.json)。

## 5. 手机尺寸浏览器与时效

使用实际桌面 Chrome 的 `390×844` mobile viewport，从真实内网访问板卡，并执行实际图像解码；不是实体手机，也不是 Qt `grabToImage`。

[最终包浏览器记录](host/browser-summary-3.json)有 4 个可见图像样本，640×480 JPEG 成功解码，generation/epoch 与相机 session 对应；可见时 `now<expires`。第 5 个样本到期后 `visibility=hidden`、expires 0，状态为“画面已过期，等待新鲜结果”。人为将浏览器设为断网后，图像保持隐藏，状态提示重试。[时效分析](browser-safety-analysis.json)与[断网截图](host/offline-mobile-3.png)分别保留。

原测试要求 5 个采样点全部可见，所以该次原始 `passed=false`。**手机页面持续无空档未验收通过**，没有调整 TTL 来让断言通过；请求/输出变慢时，页面优先清除过期图。后续过晚的浏览器 probe 没等到实时图，主相机测试已执行退出；其原失败也保留在 [browser-summary-4.json](host/browser-summary-4.json)。首个 probe 等待失败及 r2 的成功另按 [记录 1](host/browser-summary.json)、[记录 2](host/browser-summary-2.json)留存。

本机 Node 网页回归另外验证请求挂起、迟到解码、请求耗时扣除和新 generation；HTTP 回归验证共用 JPEG/帧身份、只读/私有拒绝、stop/TTL/epoch、端口冲突、流上限和释放。它们是软件边界测试，不替代实体手机或屏幕观察。

## 6. init、图标与实际重启边界

最终入口为 `/etc/init.d/S99zhelmet-app → /userdata/helmet-validation/20261115-native-app-v5-r3-lan-3/src/app/autostart.sh`。实际 `rcS` 遍历 `/etc/init.d/S??*` 并执行非 `.sh` 名称的 `start`，本入口满足调用关系。启动后台待机，不生成初始 Qt，不自动检测；重复 start 保持既有窗口和检测 intent。

迁移在旧实例 completed、进程和锁均释放后执行；只改两个项目 App 链接、两个 desktop 和本项目 init 链接。旧 cfg、desktop、入口/init 链接目标备份在 `/userdata/helmet-validation/20261115-app-lan-validation-3/migration-backup`。[迁移收据](board-v5-r3/migration.json)。`apk3.cfg` 前后 SHA 均为 `f64a8e2f751ff777b98a36a13a0ad062e0812f2bd6ebd7914a512b772f507b11`，systemui PID 保持 `740`。

用户此前明确“systemui 重启由人工决定”，本 agent 没有下发 S50systemui restart 或 reboot。相机/待机实验时 boot ID 为 `22011579-2e90-4b4a-8546-a522f7e23ca2`；最终复核观察到 `4b97345c-28ec-45e2-a391-d37b4ba6381e`，说明期间发生实际重启。init 链接重启后仍存在并指向 r3。

观察到重启后的 session `37feafe205d94e62b690be8511ee425c`，manager/service/Qt 为 `1577/1579/1582`，640 模型与 `/dev/video10` 正常检测；随后通过完整退出，service exit0、cleanup/lease true。[启动](board-v5-r3/post-boot-start.json)、[ready](board-v5-r3/post-boot-ready.json)、[退出](board-v5-r3/post-boot-terminal.json)。其 start 记录已含 visible=true、sequence2 和 Qt，不能证明是 init 在人工图标操作前创建的纯后台会话。**实际重启及之后 App 使用已观察；无人操作的开机后台待机仍未隔离验收。** 候选工作区的较小 monotonic 时间也不能单独赋予 boot ID。

最终 r3 的无窗口后台短测为 `181.151180338s / 36样本`，session `f4184798e52f49ad9a8d137ee01b956e`、manager/service `19510/19512`，0 owner变化/失败/Qt或检测启动。[idle-summary.json](board-v5-r3/idle-summary.json)。该会话之后被系统重启终止，不补写正常退出收据。最终保留观察到的正常退出状态，不主动重启检测；[final-ready.json](board-v5-r3/final-ready.json)说明重新点图标或下次 init 启动后页面才可访问。

## 7. 回归与源码 SHA

| 验证 | 结果 | 证据 |
| --- | --- | --- |
| 主机主树 | 423 项，81.096s，OK / 15 skipped | [main-r3.log](host/main-r3.log) |
| 主机 legacy | 25 项，4.091s，OK / 1 skipped | [legacy-r3.log](host/legacy-r3.log) |
| 板端 Linux fixture | 10 项，6.105s，OK / 0 skipped | [kernel-record-green3.log](board-v5-r3/kernel-record-green3.log) |

主树 15 个 skip 为 Linux resident 10、WiFi Unix 2、Windows symlink 权限 3；legacy 1 个 skip 是未设置 `RK3568_TEST_MODEL` 的真实 NPU CLI 集成。fixture 只练习 flock、继承 FD、peer credential、进程与记录竞争；真实相机/NPU证据来自上面的单独运行。

| 文件 | 修改前 SHA256 | 最终 SHA256 |
| --- | --- | --- |
| Main.qml | `7ed2edc4c37cd5067e1785dc4f06d8c7ecc6d716c9cdfaebf1aa7e92951bb93e` | `7ed2edc4c37cd5067e1785dc4f06d8c7ecc6d716c9cdfaebf1aa7e92951bb93e` |
| service.py | `d22000a1060a8c4a584661719b3825715a19b855267ab7772b376f3ad43e8c8f` | `f26df15c0530a42f6d713d8a355f90b801fce052a37769668ba944a0216f453c` |
| resident.py | `ccd64e9043005b27d9174f4fea0f79ab5796a95dc511e5b910daed260540d23f` | `60e3f21929f46fff9084d39c3a558fda075bffc66785eb7ffbec70271e3f1e95` |
| launch.py | `4428774d243396a4c2994a13460ec5f46ec9c2c1f8542a2d182f3f74c642fc52` | `6dd08e9b1ce0813b7d689aa690b3a2beb9501a3dfab08d9497f42ab5f7b96d9f` |

完整 [source-sha256.json](source-sha256.json)还记录 wifi、Pipeline、源、后处理、报警、输出均未变化。原 v4-r2 ZIP 仍为 `14b56543f7aca0b26c61b663770f8bf54729d0ba61a1ac5069f4f1143e084251`，旧发布清单没有回写。

## 8. 保留限制

实体手机、无人操作的自动后台开机、手指动作直接观察、独立供电、App WiFi 认证/DHCP/保存、长期现场、当前 App 1800 秒 gate 和声音/事件质量仍未补验。实际重启与之后的 App 启动/正常退出有记录；持续无空档采样未通过。短跑不替代 v2 30 分钟回放，也不证明长期无泄漏、工业认证或独立 85% 精度。

源码、最终 ZIP、清单、日志、摘要和版本 tag 同步 GitHub。JSONL、实时 JPEG 及重复候选 ZIP 保留在本机/板端，克隆 GitHub 不代表拿到全部原始载荷；密码、PSK、token、私钥、运行中私有 QML wrapper 不纳入发布或证据导出。
