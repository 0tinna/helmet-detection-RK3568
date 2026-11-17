# v6-r2 原生 App 可用性修复与最终验收

**结论：** v6-r2 已闭合“回到桌面后再次点图标闪退”和“空闲页不会自动发现晚插入输入源”两个可用性问题。最终完整包已重新解包到板端新根，项目自有的检测入口、WiFi 入口和 BusyBox init 已迁移；程序化五轮隐藏/唤回、双入口同实例复用、正常退出和完整回归均通过。

本轮当前板端没有 USB UVC 设备，`/dev/video0` 至 `/dev/video9` 全部是 `rkisp*`，`/dev/video10`、`/dev/video11` 不存在。因此真实 USB 摄像头热插拔、物理板屏手指点击和人工闪烁观察仍是未验项。自动重扫逻辑由回归测试和行为测试覆盖，不能替代真实热插拔。

## 1. 版本与身份

| 项目 | 值 |
| --- | --- |
| 应用版本 | `20261117-v6-r2` |
| Git tag | `native-app-v6-r2` |
| 板端根 | `/userdata/helmet-validation/20261117-native-app-v6-r2` |
| ZIP | [helmet-native-app-20261117-v6-r2.zip](../../../../dist/helmet-native-app-20261117-v6-r2.zip) |
| ZIP 大小 | `39,236,091 B` |
| ZIP SHA256 | `e66dc2983ca28258f28507c16a1be0b3aea637136166fce8c0c125b2a474c2bc` |
| 条目 | `41`，其中 `40` 个载荷、`1` 个内部 `manifest.json` |
| 五文件 code identity | `c40bbf1b2868f51b1d95b32492b7ec3ecafcf9e3432cc2c82e1e938251916d0b` |
| 外部清单 | [bundle-manifest-v6-r2.json](bundle-manifest-v6-r2.json) |
| 安装与入口收据 | [installed-release-v6-r2.json](installed-release-v6-r2.json) |

五文件对照：

| 文件 | v6-r1 基线 SHA256 | v6-r2 SHA256 |
| --- | --- | --- |
| `resident.py` | `13b34f7fcfe2eaed6b390a447f0923e61a1937685fbcc67b73bfa5d3893ab6bc` | 同左，未改 |
| `launch.py` | `6dd08e9b1ce0813b7d689aa690b3a2beb9501a3dfab08d9497f42ab5f7b96d9f` | 同左，未改 |
| `service.py` | `db0c579d93c8d60dc48e755471e7649e9c1411a53c1237afbff2cc47044edc3a` | 同左，未改 |
| `wifi.py` | `648b714137b18538f8f8e6a355565b07aec8fb01270c3deed5c01eba30249468` | 同左，未改 |
| `Main.qml` | `945a2816b88fc9f8880c13bd1ee424d1d6552f5b8927159f01fc3061dfe84c6a` | `04f6da85406ab2ee17495e78c17fdfdcf146aa8c8183045099efbb4e3390ce77` |

机器可读对照见 [code-sha256-comparison.json](code-sha256-comparison.json)。

## 2. 根因与修复

### 2.1 回桌面后再次点图标闪退

板端 UI 日志反复出现：

```text
Conflicting properties 'visible' and 'visibility' for Window 'window'
```

`Main.qml` 同时绑定 `visible` 和 `visibility: Window.FullScreen`。隐藏后再唤回时，两套窗口可见性状态争夺同一属性，形成稳定性风险。

v6-r2 只保留 `visible: false` 作为初始状态所有者，新增统一的 `presentApplication()`：

```qml
window.showFullScreen()
window.raise()
window.requestActivate()
```

常驻 show 与非常驻首次启动都通过该函数进入全屏。最终 UI 日志只保留无关的 `/var/run` 符号链接提示，不再出现可见性冲突。

### 2.2 输入源很久识别不到实时摄像头

本轮同时存在两个事实：

1. 当前板端没有 USB UVC capture 节点，摄像头在系统层确实不存在。
2. 原 UI 只在显式配置或手动刷新时重新枚举，设备晚插入后不会自动出现。

输入页现在增加了以下行为：

- 窗口可见、检测页空闲、输入下拉框未打开时，每 `2000 ms` 请求一次 `/api/config`；
- 刷新时保留当前选择；设备临时消失后再出现，会恢复之前选中的设备 ID；
- 刷新不设置 `configPending`，不会让“开始检测”无故变成不可用；
- 发现 camera 类型输入时显示绿色提示；没有实时摄像头时明确显示“尚未发现 USB 实时摄像头；设备接入后约 2 秒自动刷新”。

最终板端枚举见 [video4linux-at-acceptance.json](video4linux-at-acceptance.json)：当前只有 `/dev/video0` 至 `/dev/video9` 的 rkisp 节点，`capture_candidates` 为空。

## 3. 板端常驻与退出验收

最终源码、最终 ZIP 解包根和板端安装根使用同一五文件 identity。验收使用 `helmet_416x416.rknn`、`1.jpg`、`responsive` 回放；它是真实板端 NPU 运行，但不是物理 USB 摄像头输入。

| 项目 | 结果 |
| --- | --- |
| session | `e2398cb4ea804019a2f09bc29058a1dd` |
| manager / service / Qt PID | `27153 / 27155 / 27158` |
| UI instance | `1c6c90ed4d174c83a9ce9e60dccb7070`，五轮未变 |
| 隐藏/显示帧号 | `51 → 132 → 212 → 264 → 345 → 377` |
| 隐藏期间 | `frame_id` 持续增长；预览请求计数保持不变 |
| 停止检测 | `running=false`，最终 `frame_id=379` |
| 完整退出 | `service_exit=0`、`cleanup_completed=true`、`resource_lease_released=true` |
| 退出后 | manager/service/Qt 均为空，控制 socket 不存在 |

完整机器可读结果见 [bugfix-acceptance.json](bugfix-acceptance.json)。本轮动作由 resident 控制通道驱动，等价于点击隐藏/唤回入口；没有把它写成“人工手指在物理屏幕上点击通过”。

## 4. 图标入口与开机入口

最终项目自有入口已切换到 v6-r2：

```text
/opt/ui/src/apps/helmet_app
/opt/ui/src/apps/helmet_wifi
/etc/init.d/S99zhelmet-app
```

迁移前备份在：

```text
/userdata/helmet-validation/entry-backups/20261117-v6-r2-pre
```

入口验收 session 为 `4e0c7fdaf4314cf48fd076b360d5761c`。初始化后台实例后：

1. 检测图标打开检测页；
2. WiFi 图标切到 WiFi 页；
3. 再点检测图标切回检测页。

三次操作保持同一 session、同一 UI instance 和同一 Qt PID。`systemui` PID 始终为 `738`，没有被本项目重启；`apk3.cfg` SHA 前后都为 `f64a8e2f751ff777b98a36a13a0ad062e0812f2bd6ebd7914a512b772f507b11`。入口验收见 [entry-acceptance.json](entry-acceptance.json)。

厂商主界面是否立即显示新图标仍需人工决定是否执行：

```sh
sh /etc/init.d/S50systemui restart
```

本轮没有执行该命令。

## 5. 发布包失败与修正

首个 v6-r2 ZIP 的入口内容正确，但归档项被标记为 Windows 来源。BusyBox `unzip` 将本应 `0755` 的 shell 入口落成 `0600`，不能直接执行。该 ZIP 作为失败候选保留：

- SHA256：`1f6083aa7526c4bd1c8d25fd2b10f1fa26d6823d6b399fe49f585df74d53efe9`
- 本地：`dist/helmet-native-app-20261117-v6-r2-rejected-zip-mode.zip`
- 清单：[bundle-manifest-v6-r2-rejected-zip-mode.json](bundle-manifest-v6-r2-rejected-zip-mode.json)

修复先新增会失败的回归测试，再把 ZIP 项改为 Unix `create_system=3` 和普通文件类型位。最终归档内三个 shell 入口均标记 `0755`。在 root 的 `umask 077` 下解包后实际模式为 `0700`，对 root 启动的厂商入口仍可执行。

## 6. 回归

| 套件 | 结果 | 原始日志 |
| --- | --- | --- |
| `tests/` | `440` 项，`OK (skipped=15)`，`78.063 s` | [regression-host-v6-r2.log](regression-host-v6-r2.log) |
| `helmet/tests/` | `25` 项，`OK (skipped=1)`，`3.016 s` | [regression-legacy-v6-r2.log](regression-legacy-v6-r2.log) |

新增覆盖包括：

- 窗口只有一个可见性状态所有者；
- 常驻 show 调用 fullscreen、raise 和 activate；
- 空闲输入源定期重扫；
- 热插拔后保留原选择并可恢复；
- 自动刷新不阻塞 Start；
- ZIP shell 入口使用 Unix 可执行模式；
- v6-r2 wrapper 指向最终安装根。

## 7. 未验项

- 当前没有 USB UVC 设备，真实插入/拔出后的自动出现和 1 秒 TTL 清屏未在 v6-r2 重测；历史 v4/v5 证据保持原版本。
- 物理板屏上的手指点击、完整全屏边界和人工闪烁观察未在本轮记录。
- 实体内网手机、真实 WiFi 认证/DHCP/保存、独立供电和长期现场未在本轮完成。
- 本轮没有重跑 v6-r1 的 180 秒性能基线和 1800 秒长跑；不能把 v6-r1 数字改记为 v6-r2 新成绩。
- 模型、类别顺序、conf `0.35`、NMS `0.45`、skip `0`、TTL `1 s` 和 Pipeline 均未修改。
