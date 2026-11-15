# RK3568 原生 App v4-r2 常驻与图标迁移终验

> 日期：2026-11-15  
> 结论：Phase 1、Phase 2 六项核心验收和项目自有图标迁移均通过。正式入口已指向 v4-r2；厂商 `systemui` 未重启，人工确认主界面图标加载仍待用户完成。

## 1. 交付范围

本轮解决两类问题：

1. 检测预览在换帧时被主动隐藏，造成视频与占位文字约 5 Hz 交替；窗口未覆盖完整 720×1280 屏幕。
2. resident 监督循环读取每 250 ms 原子替换的 `service-state.json` 时，可能打开刚被 unlink 的旧 inode，并把合法快照误判为 `unsafe private file descriptor`，导致整个 App 进入 `failed`。

最终运行包：

| 项目 | 值 |
|---|---|
| 发布 ZIP | [`dist/helmet-native-app-20261115-v4-r2.zip`](../../../../dist/helmet-native-app-20261115-v4-r2.zip) |
| ZIP SHA256 | `14b56543f7aca0b26c61b663770f8bf54729d0ba61a1ac5069f4f1143e084251` |
| ZIP bytes | `39,227,461` |
| ZIP entries | `35`，其中 34 个 payload，1 个内部 `manifest.json` |
| 外部清单 | [`bundle-manifest-v4-r2.json`](../native-app/bundle-manifest-v4-r2.json) |
| 板端最终根 | `/userdata/helmet-validation/20261115-native-app-v4-r2-local-2` |
| 五文件 `code_identity` | `7cad142a4c5610dca2110fe7c22342c870317e322f28817ee1ccf9199b312302` |
| 日常入口 | `/opt/ui/src/apps/helmet_app`、`/opt/ui/src/apps/helmet_wifi` |

原 v4 ZIP、原 v4 root、v1/v2/v3 包与历史 manifest 均未覆盖。

## 2. 源码与测试变更

### 2.1 竞态修复

`Coordinator.read_json()` 只在出现 `unsafe private file descriptor` 时重新打开当前目录项，最多 3 次。文件类型、owner、mode、硬链接、symlink 与路径祖先检查没有放宽。

新增回归测试：

- [`tests/test_app_resident.py`](../../../../tests/test_app_resident.py:930)
- `test_record_read_retries_when_atomic_replace_unlinks_the_open_snapshot`

该测试先稳定复现原异常，再在修复后通过。

### 2.2 测试隔离修正

[`tests/test_app_service.py`](../../../../tests/test_app_service.py:701) 的“运行输入被移除”fixture 现在显式 mock `_camera_sources()`，避免测试宿主真实 `/dev/video10` 改变期望列表。摄像头枚举本身仍由独立的 sysfs fixture 覆盖，产品枚举逻辑未改。

### 2.3 SHA256 对照

| 文件 | 改动前 | 当前 |
|---|---|---|
| `Main.qml` | `7ed2edc4c37cd5067e1785dc4f06d8c7ecc6d716c9cdfaebf1aa7e92951bb93e` | 同左，未改 |
| `service.py` | `d22000a1060a8c4a584661719b3825715a19b855267ab7772b376f3ad43e8c8f` | 同左，未改 |
| `resident.py` | `a8490e30eff245158b789b824b6c76f8653b70debc04fdda52fb41d4e431ce3b` | `ccd64e9043005b27d9174f4fea0f79ab5796a95dc511e5b910daed260540d23f` |
| `launch.py` | `4428774d243396a4c2994a13460ec5f46ec9c2c1f8542a2d182f3f74c642fc52` | 同左，未改 |
| `wifi.py` | `648b714137b18538f8f8e6a355565b07aec8fb01270c3deed5c01eba30249468` | 同左，未改 |
| `tests/test_app_resident.py` | 无新竞态用例 | `b7609c604e5735bf33e7cc0295800b1e0c29ee8c238beaf706251fbd7385b82f` |
| `tests/test_app_service.py` | 依赖宿主摄像头列表 | `b115e94d3df7d6a2fd3f3e8f79d8bcfc7c82ae73cd3708e9e943e9a165cdc37c` |

## 3. 回归结果

在板端独立根 `/userdata/helmet-validation/full-regression-v4-r2-2` 执行：

```sh
PYTHONPATH=/userdata/helmet-validation/20261114-final/.deps \
  python3 -m unittest discover -s tests -v
```

结果：

```text
Ran 399 tests in 139.731s
OK (skipped=13)
```

完整日志：[`full-regression-v4-r2-2-final.log`](formal-1/full-regression-v4-r2-2-final.log)，SHA256为`2e94f15a1d80f5f581ee729d12a82755c3cbf932d0b530efce1c6c4c0270b5bd`。

首轮全量运行曾得到 `failures=1, errors=2`，原因是测试环境缺 `matplotlib` 以及真实摄像头污染静态输入 fixture；失败日志保留为 `full-regression-v4-r2-2.log`。修复后重新完整执行，没有沿用旧结果。

## 4. Phase 1 验收

Phase 1 证据见 [`native-app-usability-v4-phase1-1/report.md`](../native-app-usability-v4-phase1-1/report.md)。

| 验收项 | 结果 | 证据 |
|---|---|---|
| 检测中连续画面 | PASS | 300 帧实际 DRM 视频，预览空白帧 0 |
| 停止后恢复占位文字 | PASS | `stopped-placeholder-final.png` |
| 物理拔摄像头后 1 秒内清空 | PASS | 约 100 ms 清空，低于 1 秒 TTL |
| 720×1280 全屏、边缘不退回桌面 | PASS | 全屏抓图与边缘点击记录 |
| 回归测试 | PASS | 当轮 398 tests OK；修复后新全量 399 tests OK |

Phase 1 临时修改的 `/userdata/helmet-validation/20261115-native-app-v2` 为板端验证副本，已偏离冻结 v2 SHA；该临时修改未纳入 v4-r2 发布包。

## 5. Phase 2 六项验收

正式 session A：`d9fce8dd35d54c8e9f00ef2219b8a12f`。

| 验收项 | 结果 | 关键证据 |
|---|---|---|
| 点图标后全屏出现 | PASS | manager/service/Qt PID `10681/10683/10707`；[`open-1-summary.json`](formal-1/open-1-summary.json) |
| 回到桌面后进程仍在且帧号增长 | PASS | 隐藏 31 个样本，`running=true/valid=true`，帧号 `989→1942`；[`hidden-samples-31s.jsonl`](formal-1/hidden-samples-31s.jsonl) |
| 隐藏期间暂停预览刷新 | PASS | QML ACK 的 frame requests/decodes/presentations 均为 `204/203/203`，前后不变；[`hide-summary.json`](formal-1/hide-summary.json) |
| 再点图标复用同一实例 | PASS | session 与三 PID 全同，唤回帧号 `2674`；[`open-2-summary.json`](formal-1/open-2-summary.json) |
| 两图标互点只切页 | PASS | WiFi ACK tab=`wifi`，检测 ACK tab=`detection`，session/PID 不变；[`icon-switch-summary.json`](formal-1/icon-switch-summary.json) |
| UI 退出并完整清理 | PASS | service exit 0，`cleanup_completed=true`，`resource_lease_released=true`；[`ui-quit-summary.json`](formal-1/ui-quit-summary.json) |

检测配置固定为：

- 模型：`helmet_416x416.rknn`
- 输入：`/dev/video10`，`UNIQUESKY_CAR_CAMERA`
- Profile：`responsive`
- 类别顺序：`person, hat`
- conf `0.35`、NMS `0.45`、skip `0`、TTL `1 s`

## 6. 180 秒短跑

退出后通过图标重启，新 session：

`cce9979a86c5499fb43d456353290f75`

进程：

- manager `12880`
- service `12882`
- Qt `12906`

采样结果：

| 指标 | 值 |
|---|---|
| 实际采样时长 | `196.667612929 s` |
| 样本数 | `181` |
| 起始帧号 | `527` |
| 结束帧号 | `6312` |
| 帧号倒退 | `0` |
| `running=false` | `0` |
| `valid=false` | `0` |
| generation 数 | `1` |
| source epoch 数 | `1` |
| 进程变化 | `0` |
| 停止前最终帧号 | `6685` |

原始样本：[`longrun-samples-181s.jsonl`](formal-1/longrun-samples-181s.jsonl)  
汇总：[`longrun-summary.json`](formal-1/longrun-summary.json)  
停止状态：[`longrun-stop-status.json`](formal-1/longrun-stop-status.json)  
最终清理：[`longrun-cleanup-summary.json`](formal-1/longrun-cleanup-summary.json)

停止与退出均通过真实 UI 控件触发。结束后 NPU 回到 `0%`。

## 7. 图标迁移

迁移只修改本项目拥有的四个链接和两个 desktop：

- `/opt/ui/src/apps/helmet_app`
- `/opt/ui/src/apps/helmet_wifi`
- `/opt/ui/src/appicons/helmet_app.svg`
- `/opt/ui/src/appicons/helmet_wifi.svg`
- `~/.local/share/applications/helmet-detection.desktop`
- `~/.local/share/applications/helmet-wifi.desktop`

迁移结果：

- 四个链接全部指向 v4-r2 根。
- 两个 desktop 的 `Exec` 与 `Icon` 全部指向 v4-r2。
- `apk3.cfg` 未改，前后 SHA 均为 `f64a8e2f751ff777b98a36a13a0ad062e0812f2bd6ebd7914a512b772f507b11`。
- 原始 cfg、链接记录和两个 desktop 已备份。
- `systemui` 未重启。
- `human_icon_tap=false`：主界面重新加载后的实体图标人工点击仍待用户执行。

备份路径：

`/userdata/helmet-validation/20261115-native-app-v4-r2-local-2/vendor-entry/icon-migration-r2-20261115T055253Z-d83cbd10`

迁移收据：[`icon-migration-result.json`](formal-1/icon-migration-result.json)。

迁移后直接执行正式入口 `/opt/ui/src/apps/helmet_app`，新 session `95d62a7ffe384404b9c7ef3df4f32865` 正常启动；UI 退出为 `phase=completed`、service exit 0、三进程全消失。证据：[`post-migration-cleanup-retry.json`](formal-1/post-migration-cleanup-retry.json)。

另一次链接启动中，WiFi 页切换后立即退出出现 `service_exit=-15`，但进程与 lease 均清理。该失败收据保留为 [`post-migration-cleanup.json`](formal-1/post-migration-cleanup.json)，没有用它替代上面的正常退出结果。

## 8. 已保留的失败与诊断

1. 原子状态替换竞态：原始 `ValueError: unsafe private file descriptor` traceback 和修复前失败证据保留。
2. 第一次 v4-r2 候选的 shell wrapper 使用 CRLF，启动时出现 `/bin/sh^M`。该包被拒绝并保存；最终包 wrapper 为 LF。
3. 早期自动点击使用 `y=1200/1201`，落在实际按钮范围 `1129..1192` 下方，没有触发按钮。该误点证据保留；正式验收使用 `(185,1165)`。
4. Qt 最小按钮探针证明 upper/bottom `onClicked` 均正常，排除了系统层吞点击。
5. 历史 `failed-stuck-exiting-*`、touch mapping 与 resident trace 目录仍在板端实验根，没有被覆盖。

## 9. 未验项与已知限制

- 厂商 `systemui` 重启后的人眼图标显示和手指点击未执行；当前只证明正式链接和包装器可启动。
- U 盘安装、无线 OTA、无人值守升级未实测。已确认的是完整包 SCP/复制到新根、清单校验和整体解压部署。
- WiFi App 已有扫描、SSID 与手动认证 UI；本轮没有验证真实目标网络认证、DHCP、保存配置和长期断网恢复。
- 未做独立供电、拔掉主机网线后的长期现场运行或工业安全认证。
- 本轮常驻 gate 为 180 秒短测，不是原计划的 1800 秒 gate。
- 离线子网 namespace 验证延后；本次通过本机检测、真实摄像头与 NPU 证明日常检测路径，不把短测扩展成空白板/无网络 OS 安装证明。
- 输入自动化使用有界 `uinput` absolute pointer；它驱动的是实际 Qt 控件与 HTTP 生命周期，但不是人工手指触摸。Phase 1 的物理触摸/拔线观察另见其报告。

## 10. 最终判断

“能正常看画面、不会误退桌面、断流不残留旧帧”的 Phase 1 已通过。

“点图标全屏出现、回桌面常驻、再次点击复用、双图标切页、UI 退出清理、180 秒连续检测”的 Phase 2 六项已通过。日常检测可完全离线运行，不需要板端浏览器或持续 USB 连接电脑；更新仍需要完整包复制/部署，WiFi 真实连接与长期现场验收另列。
