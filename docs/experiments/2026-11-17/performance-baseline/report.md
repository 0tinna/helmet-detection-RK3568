# 原生 App 性能优化与 v6-r1 最终验收

本报告记录 2026-11-17 的预览热路径和常驻状态写优化。结论只适用于同板、同 boot、`1.jpg` 回放、`helmet_416x416.rknn`、`responsive` 的受控条件；USB 摄像头当前未接入，因此物理相机 FPS、板屏人工连续性和拔线 TTL 没有用本轮回放数据代替。

## 1. 结论

主性能目标已经完成，并形成最终 v6-r1：

| 指标 | v5-r3 基线 | v6-perf7 | 变化 |
| --- | ---: | ---: | ---: |
| 可见态检测 FPS | 7.45 | **7.80** | +0.35 |
| 可见态呈现 FPS | 4.30 | **4.40** | +0.10 |
| Qt CPU | 56.50% | **33.25%** | -23.25 个百分点 |
| 单次预览载荷 | 447,362 B JSON | **195,725 B**（195,474 B JPEG + 251 B 元数据） | -56.25% 总载荷 |
| 隐藏态检测 FPS | 11.10 | **11.45** | +0.35 |
| owner `record.json` 写速 | 8.20 次/秒 | **0.20 次/秒** | -97.56% |
| service-state 写速 | 3.70 次/秒 | **0.90 次/秒** | -75.68% |

180 秒回放继续通过：180 个采样、检测 8.08 FPS、呈现 4.41 FPS、Qt CPU 33.46%，`frame_id` 从 39 单调增长到 3127。该结果证明当前软件和服务热路径可连续运行，不证明物理相机或实体屏幕已通过。

## 2. 根因与修改

旧热路径每帧把约 335 KB JPEG 编成约 447 KB base64 JSON，再交给 Qt 解码；同时 manager 与 service 每秒多次原子替换并同步状态文件。可见态 Qt CPU 达 56.50%，检测从隐藏态 11.10 FPS 降到 7.45 FPS。

本版做了四项闭合修改：

1. `service.py` 缓存最近 4 个不可变 JPEG 包。每个包有随机 `frame_token`，`/api/preview-meta` 只传帧身份和小元数据，`/api/frame?frame_token=...` 直接返回精确 JPEG。
2. `Main.qml` 用两个交替 `Image` 加载二进制 JPEG。旧有效层保持显示到新层 `Image.Ready` 且全部 freshness 校验通过；两个层设 `cache:false`，避免不断累积令牌 URL 解码缓存。
3. LAN 页面改为 `/status.json` 加精确 `/frame.jpg?frame_token=...`。浏览器先完成离屏解码，再绘制到 canvas，换图期间不清空上一张有效图。
4. `resident.py` 和 `service.py` 合并重复状态写：owner record 只在语义变化、5 秒心跳或终态时写；service receipt 空闲时最多每秒一次，终态强制写。

保留的校验包括 `generation`、`source_epoch`、`frameExpires`、`minimumCaptureAt`、`detection.valid` 和 `detection.running`。令牌淘汰、TTL 到期、epoch 或 run 变化后不会回退到“最新帧”。conf `0.35`、NMS `0.45`、skip `0`、TTL `1 s`、模型、Pipeline 和 `yolo_post.py` 均未改变。

## 3. 同条件性能证据

| 实验 | 文件 | 检测 FPS | 呈现 FPS | Qt CPU | 解码/呈现 |
| --- | --- | ---: | ---: | ---: | --- |
| v5-r3 可见 | [baseline-visible-416.json](baseline-visible-416.json) | 7.45 | 4.30 | 56.50% | 86 / 86 |
| v6-perf3 可见 | [v6-perf3-visible-416.json](v6-perf3-visible-416.json) | 6.95 | 3.80 | 34.50% | 76 / 76 |
| **v6-perf7 可见** | [v6-perf7-visible-416.json](v6-perf7-visible-416.json) | **7.80** | **4.40** | **33.25%** | **88 / 88** |
| v5-r3 隐藏 | [baseline-hidden-416.json](baseline-hidden-416.json) | 11.10 | 0 | 2.65% | 0 / 0 |
| **v6-perf7 隐藏** | [v6-perf7-hidden-416.json](v6-perf7-hidden-416.json) | **11.45** | 0 | **2.50%** | 0 / 0 |

perf7 可见实验共 88 次请求、88 次解码和 88 次呈现，请求与呈现一一闭合。perf3 的请求数高于呈现数，说明当时仍有解码/present 丢失；后续修复和轮询调整解决了该断点。

## 4. 180 秒回放与常驻

[v6-perf7-longrun-180-visible.json](v6-perf7-longrun-180-visible.json)：

- 运行 180.001 秒，180 个采样。
- 检测约 8.08 FPS，呈现约 4.41 FPS。
- 795 次预览请求、793 次解码、793 次呈现。
- `frame_id` 39 → 3127，采样单调，无崩溃或短跑中的进程重建。
- Qt CPU 约 33.46%。

[resident-lan-acceptance.json](resident-lan-acceptance.json) 记录同一 session `6687faa972764e01996e985a6593fad2` 的首开、隐藏、复开、双页切换和退出：

- manager/service/Qt PID 为 `17214/17216/17219`，隐藏 6 秒后三者不变。
- 隐藏期间 `frame_id` 143 → 260，增加 117 帧；预览请求计数 13 → 13，说明隐藏时只停止预览刷新，检测和状态轮询继续。
- WiFi 页与检测页复用同一 UI instance；每次复开仍为同一 session。
- `boot.py stop` 写入 `cleanup_completed=true`、`resource_lease_released=true`、`service_exit=0`，控制 socket 消失，三个进程结束。
- 退出后重新打开得到新 session `a6e91ddfe97047e4933be1f36426895a`，随后再次完整退出。

## 5. LAN 与开机入口

板端访问 `192.168.1.3:8091/status.json` 和独立 Windows 主机访问同一地址均返回 HTTP 200；Windows 主机根页面也为 200。状态页经精确令牌取得的 JPEG 为 190,701 B，`Content-Type=image/jpeg` 且 JPEG 首尾标记有效。非法令牌返回 400，已验证旧令牌在 TTL 后返回 503。实体手机和手指操作仍未验证。

最终整包已经部署到 `/userdata/helmet-validation/20261117-native-app-v6-r1`，并只迁移本项目拥有的两条 `/opt/ui/src/apps/helmet_*` 链接、两份 desktop 与 `/etc/init.d/S99zhelmet-app`。厂商 `apk3.cfg` SHA 仍为 `f64a8e2f751ff777b98a36a13a0ad062e0812f2bd6ebd7914a512b772f507b11`；迁移前备份保留，`systemui` PID 仍为 738，未由本项目重启。

| 交付项 | 最终身份 |
| --- | --- |
| ZIP | [helmet-native-app-20261117-v6-r1.zip](../../../../dist/helmet-native-app-20261117-v6-r1.zip) |
| 大小 / entries | 39,234,949 B；41 entries（40 payload 与内部 manifest） |
| ZIP SHA256 | `8b51ab3dded637e02b543051a8f77427df91359b12fb6b42187d88b9a33aa726` |
| 五文件 code identity | `86f191feb7adf6c8b0bafc30198cf2be2f7aa1a0bcba0161c1e5b8240a59a453` |
| 发布清单 | [bundle-manifest-v6-r1.json](bundle-manifest-v6-r1.json) |
| 安装收据 | [installed-release-v6-r1.json](installed-release-v6-r1.json) |
| 入口迁移 | [entry-migration.json](entry-migration.json) |
| 入口验收 | [entry-acceptance.json](entry-acceptance.json) |

[entry-acceptance.json](entry-acceptance.json) 从最终入口完成以下链路：init 后台启动无 UI、不自动检测，`8091` 待机可访问；首次打开得到 session `f556d375aeee4f82b36fc579908963aa` 和 PID `18272/18274/18280`；两页复用同一 UI instance，`frame_id 3→248`；`init stop` 写入 `service_exit=0`、`cleanup_completed=true`、`resource_lease_released=true`，socket 和三进程消失；再次 start 得到新 session `681af023d5c64391bd5dd26c55dd5a26`，随后正常 stop。该验收覆盖真实入口命令链，不代替 `systemui` 重载后的实体图标或手指触摸。

## 6. 回归

2026-11-17 完整主机回归：

| 套件 | 结果 |
| --- | --- |
| `tests/` | **435 项，OK，15 skipped，77.514 秒**；[原始日志](regression-host-v6-r1.log) |
| `helmet/tests/` | **25 项，OK，1 skipped，3.082 秒**；[原始日志](regression-legacy-v6-r1.log) |

完整回归包含新增 release builder 测试、令牌精确查询、淘汰/TTL/epoch、QML 双 Image、无 base64 热路径、LAN 解码门、状态写合并和 resident 生命周期。NPU gated 与平台相关 skip 保留，不按性能回放推断通过。

## 7. SHA 对照

| 文件 | v5-r3 | v6-r1 |
| --- | --- | --- |
| `resident.py` | `60e3f21929f46fff9084d39c3a558fda075bffc66785eb7ffbec70271e3f1e95` | `13b34f7fcef2eaed6b390a447f0923e61a1937685fbcc67b73bfa5d3893ab6bc` |
| `launch.py` | `6dd08e9b1ce0813b7d689aa690b3a2beb9501a3dfab08d9497f42ab5f7b96d9f` | 同左，未改 |
| `service.py` | `f26df15c0530a42f6d713d8a355f90b801fce052a37769668ba944a0216f453c` | `db0c579d93c8d60dc48e755471e7649e9c1411a53c1237afbff2cc47044edc3a` |
| `wifi.py` | `648b714137b18538f8f8e6a355565b07aec8fb01270c3deed5c01eba30249468` | 同左，未改 |
| `qml/Main.qml` | `7ed2edc4c37cd5067e1785dc4f06d8c7ecc6d716c9cdfaebf1aa7e92951bb93e` | `945a2816b88fc9f8880c13bd1ee424d1d6552f5b8927159f01fc3061dfe84c6a` |

五文件 `code_identity` 为 `86f191feb7adf6c8b0bafc30198cf2be2f7aa1a0bcba0161c1e5b8240a59a453`。v6-r1 本地源码、最终 ZIP 和板端安装根的五文件 SHA 逐项一致。

## 8. 未采用与失败候选

- 首版 v6-r1 ZIP：vendor wrapper 在 Windows 封包时写成反斜杠 `/userdata/...` 路径，板端因应用完整性检查正确拒绝启动。ZIP 为 [helmet-native-app-20261117-v6-r1-rejected-wrapper.zip](../../../../dist/helmet-native-app-20261117-v6-r1-rejected-wrapper.zip)，39,234,962 B，SHA `9439f6ebbd1f434dc0b2ec9f7ca8245627f015efc86fa6d5ed4e2ed539351222`；拒绝根为 `/userdata/helmet-validation/20261117-native-app-v6-r1-rejected-wrapper`。builder 改用 `PurePosixPath.as_posix()`，并新增先复现缺失 `vendor_wrapper`、修复后通过的 release builder 测试；该测试使主套件从 434 增至 435 项。拒绝包只作失败证据，不用于安装。
- `v6-perf1`：部署权限不安全，被应用完整性校验正确拒绝，未进入性能验收。
- `v6-perf2`：二进制请求已发出，但两个 QML Image 缺少 `pendingFrameToken` 声明，呈现计数为 0；该失败保留为源码契约回归依据。
- `v6-perf4/v6-perf5`：尝试缩小服务端预览或 QML `sourceSize`，检测或呈现吞吐下降，均未采用。
- 本报告不把小尺寸预览实验的较高单点探测值拼入最终结果，也不改变模型尺寸、conf、NMS、skip 或 TTL。

## 9. 仍未验证

- USB UVC 当前未接入，`/dev/video10` 不存在；本轮没有物理摄像头 FPS。
- 没有板屏人工观察，因此软件双缓冲、请求/呈现计数和 180 秒回放只能证明逻辑连续性；物理面板闪烁需人工确认。
- 没有运行中拔线，1 秒 TTL 清屏沿用历史 v4 物理证据，不在本轮补写。
- 没有实体手机；仅完成板端自身 WLAN 地址和独立 Windows 主机 HTTP 访问。
- 没有直接人工点击图标后的 `systemui` 显示检查；开机入口只做链接、启动、状态和正常退出核验。
- 回放 180 秒不等于物理摄像头 180 秒，也不等于正式长期现场稳定性。

机读汇总见 [before-after.json](before-after.json)。
