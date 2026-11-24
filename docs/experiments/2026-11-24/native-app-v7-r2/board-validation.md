# native-app-v7-r2 板端验收

日期：2026-11-24

板端：ATK-DLRK3568，Linux 5.10.160，Python 3.10.5，Wayland/Weston，720×1280

状态：`board-core-pass`；真实 SMTP、实体手机、人工屏幕和物理多人事件仍为
`not-run`

## 1. 身份与环境

本轮先把包含 NumPy 框修复的完整候选 ZIP 解压到
`/userdata/helmet-validation/20261124-native-app-v7-r2`，没有在安装根逐个手改
业务源码。预验收 ZIP SHA256 为
`5dd332dbe5c9170470ff9b6460ffc90a06299d32c66f1a23f9b8701e1155109d`，
48 entries；正式发布包在文档收口后重新构建，最终 SHA 见
[发布清单](bundle-manifest-v7-r2.json)。

USB UVC 为 `UNIQUESKY_CAR_CAMERA`：

- 采集节点：`/dev/video10`
- metadata 节点：`/dev/video11`
- 物理摄像头使用模型：`helmet_416x416.rknn`
- 模型 SHA256：
  `7e0eefe4bfe86627bb6b58161cacca10912e0dbe1ba2513f58d091c40b26cd48`

文档收口后的最终 ZIP 又以 staging 方式完整替换了同一隔离根。最终包 SHA256
为 `4d040f6c2c162df8125c41cc002e8d14510f567704262a465d8935a3905a4f4b`，
47 个载荷的哈希和可执行位全部匹配内部 manifest。最终安装实例为 session
`4372bd00d7f94daabf6aa3b728cc3a75`，manager/service/Qt PID 为
`16669/16671/16682`，UI instance 为 `7f92a49dfecf4b54b83b8922764d71e9`。
程序化隐藏、WiFi 页唤回和检测页唤回保持同一实例；板端 `8091` 根页面、
`8092` 根页面及 `/healthz` 返回 `200`，未授权 `/events` 返回 `403`。
最终安装收据见
[installed-release-v7-r2.json](installed-release-v7-r2.json)。

## 2. 事件与内网页面

事件功能先暴露了一个仅板端真实数据类型触发的缺陷：Pipeline 提供 NumPy
检测框，跟踪器原实现只接受 `list/tuple`，因此画面有红框但事件索引始终为空。
本轮先新增失败回归测试，再把 NumPy 坐标转换为普通 Python 数值，模型、类别、
阈值、NMS、skip 和后处理均未修改。

使用归档回放图 `hard_hat_workers1006.png` 验证事件链路。该图不是物理摄像头
事件，源 SHA256 为
`27239ab95298c0b9569c8b3411c4ebaa519b8a3219d4bbce9ca404639e6a979f`。
第 1 帧建立 8 条裸头轨迹，第 2 帧确认并原子保存第一张 JPEG/JSON；事件元数据
记录 `trigger_track_ids=1..8`、`count=8`。

同网 HTTP 验收结果：

| 路由 | 结果 |
| --- | --- |
| `/healthz` | `200`, `ok` |
| `/events` 无 Cookie | `403` |
| `/api/events` 无 Cookie | `403` |
| 事件 JPEG 无 Cookie | `403` |
| 有效邀请 | `302→/events`，设置 HttpOnly Cookie |
| `/api/events` | `200`，返回 1 条事件 |
| 日期筛选 | `200`，返回 1 条事件 |
| 鉴权 JPEG | `200 image/jpeg`，40,443 B |
| `/api/status` | `200`，`scope=lan`，无 `public_ip` |
| `POST` | `405`，只读 |

完整脱敏结果见
[事件页验收 JSON](board-event-viewer-acceptance-v7-r2.json)。本轮请求来自板端
同一局域网地址，不是实体手机；页面人工视觉检查仍为 `not-run`。

## 3. 常驻、隐藏与双入口

同一物理摄像头检测过程中完成隐藏、WiFi 页唤回和检测页再唤回：

| 项目 | 结果 |
| --- | --- |
| session | `3887721c8310416e9263884130b7b8e7` |
| service PID | `12791` |
| Qt PID | `12850` |
| UI instance | `dd12457cf5824d399d4924ef09b19dfd` |
| 隐藏前帧号 | `27` |
| 隐藏 3 秒后帧号 | `132` |
| 隐藏期间帧增量 | `105` |
| 切到 WiFi 再回检测 | 同一 session/PID/UI instance |

隐藏期间预览解码计数保持冻结，检测帧号继续增长。完整摘要见
[常驻唤回 JSON](board-resident-hide-show-v7-r2.json)。该结果证明程序化
隐藏/唤回和检测连续；板屏人工触摸仍为 `not-run`。

## 4. 180 秒物理摄像头长跑

物理 UVC 连续运行目标 180 秒，自动采样 37 次，实际记录
`184.465 s`：

- 帧号 `13→5345`，增量 `5332`，严格单调增长。
- 检测约 `9.8 FPS`，采集约 `28.6 FPS`。
- `read_failure=0`、`detection_failure=0`。
- manager/service/Qt 前后均为 3 个进程，无堆积。
- 停止收据 `stopped=true`，随后有效帧清除。

完整指标见
[物理摄像头长跑 JSON](physical-camera-longrun-v7-r2.json)。该次相机画面没有
人员，不能替代真实未戴帽多人/后进入事件验收；也没有人工观察屏幕闪烁。

## 5. 断流 TTL 与恢复

通过 Linux `uvcvideo` sysfs 解绑两个接口，模拟采集设备中断：

- `0.116 s` 内状态转 `degraded`，`valid=false`。
- `/api/preview-meta` 从 `200` 变为并保持 `503`。
- 旧帧在 1 秒 TTL 内消失，没有继续作为有效画面提供。
- 重新绑定后恢复 `/dev/video10`，重新检测达到 `healthy`、帧号 `1→5`。

该测试是驱动解绑，不是人工拔线。原始时序见
[UVC 断流 TTL JSON](uvc-unbind-ttl-v7-r2.json)。

## 6. 完整退出和重新启动

第一次完整退出后，使用新入口重新启动得到新 session
`1c137b50ca8f4a8187ed4b5a10c10190`，随后再次完整退出：

- 退出前 manager/service/Qt PID：`15818/15820/15825`。
- `8091`、`8092` 和控制 socket 均存在，资源锁不可抢占。
- 退出请求真实受理，`1.103 s` 后三进程消失。
- 两个端口关闭，控制 socket 消失，资源锁可重新获取。

完整结果见
[终态清理 JSON](terminal-cleanup-v7-r2.json)。第一次 cleanup 尝试也完成了
清理，但其脚本把“退出前端口读取”放在退出之后，已明确废弃，不用于证据。

## 7. 仍未执行

- 物理摄像头一人、多人和后进入人员的真实未戴帽事件。
- 同一物理人员离开超过轨迹窗口后重新进入的新事件。
- 人工拔线而非 sysfs 解绑的最终 TTL 观察。
- 真实 SMTP 登录、学校邮箱收件、附件和内网链接。
- 同一 Wi-Fi 实体手机访问、照片加载和人工筛选。
- 板屏人工观察连续画面/闪屏、手指触摸和全屏边缘误触。
- 图标迁移、`systemui` 重启和实际整机重启自动启动。
- WiFi 扫描后的目标网络认证、DHCP、保存和失败恢复。
- 物理场景中的 7 天/容量清理效果。

这些项目不能由主机 fake SMTP、回放事件、sysfs 解绑或程序化 UI ACK 填补。
