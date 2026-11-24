# native-app-v7-r3 板端验证

日期：2026-11-24

板端：ATK-DLRK3568，aarch64，BusyBox init，Weston/Wayland，720×1280 竖屏

状态：`board-core-pass`。实体手机、真实未戴帽多人/后进入/重入、人工拔线、
板屏人工观察、`systemui` 重启和整机重启仍为 `not-run`。

## 1. 干净安装

先在 v7-r2 正常发起的退出中确认 `phase=completed`、
`cleanup_completed=true`、`resource_lease_released=true`、`service_exit=0`，
manager/service/Qt 随后消失。

v7-r3 ZIP 上传到开发板后先核对整包 SHA256 和 ZIP CRC，再解压到独立 staging
目录。板端逐文件核对 45 个文件、44 个载荷、五文件身份、告警功能身份、禁止
成员和可执行位，全部通过后才原子改名为：

```text
/userdata/helmet-validation/20261124-native-app-v7-r3
```

没有向 v7-r2 或其他旧根补文件。最终安装与身份见
[installed-release-v7-r3.json](installed-release-v7-r3.json)。

## 2. 直接内网看板

主机位于同一局域网，直接访问板端 `8092`。空事件库时，页面显示当前
`UNKNOWN // WAITING`、证据记录 `0` 和“尚未记录未戴安全帽事件”，没有把空库
解释成现场安全。

| 路由 | 结果 |
| --- | --- |
| `/` | `200`，无 Cookie，赛博朋克只读看板 |
| `/healthz` | `200 ok` |
| `/api/status` | `200`，包含 `alarm_state`、`frame_id`、`source_epoch`、`scope=lan` |
| `/api/events` | `200`，真实空库返回 `events=[]` |
| `/alerts` | `302 → /` |
| `POST /api/events` | `405` |

原始响应见
[dashboard-lan-acceptance-v7-r3.json](dashboard-lan-acceptance-v7-r3.json)。
本轮没有真实未戴帽事件，因此没有为了展示而向运行库写入 fixture；活动 JPEG
路由的真实事件请求仍为 `not-run`，主机边界测试已覆盖其错误和成功路径。

## 3. 真实摄像头和 180 秒

USB UVC 被枚举为采集节点 `/dev/video10`，服务选择：

```text
model  = helmet_640x384.rknn
source = /dev/video10
```

首次真实启动采样 16 秒，帧号 `6→423`，末态 `running=true`、`valid=true`。
随后连续采样 `180.412` 秒：

- 帧号 `1005→6317`，增量 `5312`。
- 13/13 次 `running=true`、`valid=true`、预览服务存活。
- manager/service/Qt 身份不变，没有进程堆积。
- 内部与 `8092` 的告警态 13/13 一致，公开请求的帧号自然滞后 `0–6` 帧。
- 相机画面没有裸头目标，告警真实保持 `unknown`，没有创建未戴帽事件。

原始记录见
[physical-camera-longrun-v7-r3.raw.json](physical-camera-longrun-v7-r3.raw.json)，
推导分析见
[physical-camera-longrun-v7-r3.analysis.json](physical-camera-longrun-v7-r3.analysis.json)。
原始脚本对两个顺序 HTTP 请求要求帧号逐值完全相等，因此其单一检查为 `false`；
该失败原样保留，不能用推导结果替换。

帧 ID 增长约 `29.44/s` 是状态帧号节奏，不当作独立 NPU 推理 FPS 结论。

## 4. 常驻、隐藏和双入口

同一 session 为 `96f7b07221fd4fd4bb94296dc6e6309f`：

- 隐藏前帧号 `5122`。
- 隐藏 3 秒后帧号 `5246`，增量 `124`。
- Wi‑Fi 图标唤回后，再点检测图标切回。
- session、service PID、Qt PID 和 UI instance 全部保持。
- Qt ACK 先记录不可见，最终记录可见。

原始结果见[常驻隐藏与唤回](resident-hide-show-v7-r3.json)。这证明程序化生命周期
和检测连续，不代替手指触摸或板屏人工观察。

## 5. 图标与 BusyBox 入口

迁移前已备份厂商配置、四个入口链接、两份 desktop 和 init 链接：

```text
/userdata/helmet-validation/vendor-backups/20261124-v7-r3
```

当前四个厂商入口与 init 均指向 v7-r3：

```text
/opt/ui/src/apps/helmet_app
/opt/ui/src/apps/helmet_wifi
/opt/ui/src/appicons/helmet_app.svg
/opt/ui/src/appicons/helmet_wifi.svg
/etc/init.d/S99zhelmet-app
```

`apk3.cfg` 与原备份逐字节一致，SHA256
`f64a8e2f751ff777b98a36a13a0ad062e0812f2bd6ebd7914a512b772f507b11`。
两个已有条目不需要改配置；只更新本项目拥有的链接和 desktop Exec。

`S99zhelmet-app stop` 返回真实 completed 清理后，`start` 先建立
`5951db129f474227822271fbf70fe951`，后续文档刷新包再次整体部署并得到
session `04e791c565b64324ab04ea25603da180`。文档链接修正后的最终 ZIP 再完成
一次整体部署，得到最终后台 session
`9e61e9f4a4844302ab7e9a597f264b9e`。`status` 显示 `8091` 与 `8092` 存活且
检测仍为人工启动。详见
[图标与 init 迁移](vendor-and-boot-entry-migration-v7-r3.json)和
[BusyBox 生命周期](boot-adapter-lifecycle-v7-r3.json)。

本轮没有自动重启 `systemui`，也没有重启整机。

## 6. 完整退出与再次启动

长跑会话 `96f7b07221fd4fd4bb94296dc6e6309f` 正常退出后：

- manager/service/Qt 全部消失。
- `8091`、`8092` 关闭。
- 控制 socket 消失。
- `cleanup_completed=true`、`resource_lease_released=true`、`service_exit=0`。

随后先启动 session `cdfe3449863e4bd6a2220eb581ae0fff`，再通过 BusyBox
`stop/start` 得到后台 session `5951db129f474227822271fbf70fe951`。文档刷新
后的最终完整包又经过 staging 逐文件核验、整体替换和新一轮 `stop/start`，
得到 session `04e791c565b64324ab04ea25603da180`。随后文档链接修正包再次完成
完整替换，最终后台 session 为
`9e61e9f4a4844302ab7e9a597f264b9e`。证据见
[终端清理](terminal-cleanup-v7-r3.json)与
[最终包部署后验收](final-package-postdeploy-v7-r3.json)。

## 7. 仍未执行

- 同一 Wi‑Fi 的实体手机访问、照片加载与人工筛选。
- 真实一人、多人、后进入和重入未戴帽事件。
- 人工拔 USB 摄像头后的 1 秒 TTL 屏幕观察。
- 板屏人工检查闪屏、连续画面、触摸、全屏边缘和实际图标点击。
- `systemui` 重启和实际整机重启后的访问复测。
- WiFi 目标网络手动识别、认证、DHCP、保存和失败回滚。
- 物理场景中的 7 天/容量清理。
