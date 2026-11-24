# native-app-v7-r2 内网事件站主机验证

日期：2026-11-24

状态：`software-and-board-core-pass`

板端核心验收：[通过](board-validation.md)

真实 SMTP、实体手机、人工屏幕和物理多人事件：`not-run`

## 1. 候选身份

| 项目 | 值 |
| --- | --- |
| 版本 | `20261124-v7-r2` |
| 版本标识 | `native-app-v7-r2` |
| 部署根 | `/userdata/helmet-validation/20261124-native-app-v7-r2` |
| ZIP | `dist/helmet-native-app-20261124-v7-r2.zip` |
| ZIP 字节 | `39267375` |
| ZIP SHA256 | `4d040f6c2c162df8125c41cc002e8d14510f567704262a465d8935a3905a4f4b` |
| `code_identity` | `6d1a88d5a822c6846c98aad06356e436417514c903c43b046d326338c75ee7ea` |
| `alert_feature_identity` | `75c47b78025ac0d66ee273e92d4b46c549ae3cd6883ad1c40b4ce6d451a88ba1` |
| 发布清单 | [bundle-manifest-v7-r2.json](bundle-manifest-v7-r2.json) |
| 整包核验 | [release-verification-v7-r2.json](release-verification-v7-r2.json) |

ZIP 共 48 个条目，其中 47 个 payload、1 个内部 `manifest.json`。外部清单、
内部清单、SHA sidecar 和每个载荷的 SHA256/字节数一致。ZIP 不包含
`mail.json`、`event-access.key`、旧 `public-access.key`、`app-state/`、
事件照片、运行 token、`results/helmet-events/` 或 Python 缓存。

发布清单明确记录：

```text
lan_event_port=8092
lan_only_event_viewer=true
public_event_viewer_supported=false
smtp_credentials_included=false
event_access_key_included=false
```

v7-r2 包只包含内网事件站手册，未包含历史 v7-r1 公网事件站手册。

## 2. 主机自动化结果

主测试树：

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

结果：`531` 项通过，`15` 项跳过，耗时 `101.252` 秒。完整输出见
[regression-host-v7-r2.log](regression-host-v7-r2.log)。

`helmet/tests`：

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s helmet/tests -v
```

结果：`25` 项通过，`1` 项因未设置 `RK3568_TEST_MODEL` 跳过，耗时
`3.586` 秒。完整输出见 [regression-legacy-v7-r2.log](regression-legacy-v7-r2.log)。

`helmet/` 与 v7-r2 发布构建器的 Python 编译通过，`helmet/app/deploy.ps1`
通过 PowerShell AST 语法解析。上述结果均为主机软件证据。

## 3. 软件证据

- `LanAddressResolver` 只选择本机当前路由对应的 RFC1918 IPv4；不会访问
  `ifconfig.me` 或其他外部地址服务。
- 告警邮件只包含内网 `8092` 查看地址和“手机与开发板连接同一 Wi-Fi/局域网”
  访问要求，不再记录、返回或展示公网 IP。
- `mail_probe.py` 可发送一封无事件附件的同步 SMTP 自检邮件；失败结果统一
  脱敏为 `mail_probe_failed`，不输出服务器响应正文、密码或授权码。
- `LanEventServer` 保留邀请令牌与 HttpOnly/SameSite Cookie 鉴权；根页面、
  事件 JSON、照片和状态接口均不提供实时帧或检测控制。
- 事件页显示照片、时间、事件 ID、帧号、检测状态、邮件状态、存储状态、
  分页和日期筛选；公网 IP 卡片已删除，改为 LAN 在线/离线状态。
- 同一个运行代际内的不同未戴帽轨迹分别形成事件；持续停留轨迹不会反复保存。
- 新增回归覆盖 Pipeline 实际提供的 NumPy 检测框。修复前主机画面可显示红框，
  但事件跟踪器会静默丢弃全部框；本轮在失败测试后完成最小坐标规范化，
  未修改模型、类别、阈值、NMS、skip、Pipeline 或后处理。
- JPEG/JSON 原子保存、7 天保留、256 MiB、5000 条和磁盘最低余量策略由
  主机边界测试覆盖。

这些测试使用 fake SMTP、本机 HTTP 和临时文件。它们证明代码路径与契约，
不能证明学校邮箱实际收件、实体手机可访问或板端资源长期稳定。

## 4. 板端已通过项

- 全新隔离根解压预验收完整 ZIP，并从该根启动 resident、service 和 Qt。
- 归档回放图在板端真实 NPU 生成 8 条裸头轨迹和 JPEG/JSON 事件。
- 有效邀请、Cookie、只读事件 JSON、日期筛选、JPEG 和状态接口通过；
  无 Cookie 请求均为 `403`，`/api/status` 为 `scope=lan` 且无公网字段。
- 同一 session、service PID、Qt PID 和 UI instance 完成隐藏、WiFi 页和检测页
  唤回；隐藏 3 秒检测帧号 `27→132`。
- 物理 `/dev/video10` 长跑 `184.465 s`，帧号 `13→5345`，约 `9.8` 检测 FPS；
  `read_failure=0`、`detection_failure=0`，进程数无增长。
- UVC sysfs 解绑后 `0.116 s` 内旧帧失效、预览 `503`；重新绑定后恢复检测。
- 完整退出后 manager/service/Qt、`8091`、`8092`、控制 socket 和资源锁均在
  `1.103 s` 内释放；再次启动得到新 session。

详细命令、事件 ID、帧号与限制见[板端验收](board-validation.md)。

## 5. 真实现场未验项

以下项目状态均为 `not-run`：

- ATK-DLRK3568 物理 UVC 摄像头的一人、多人、后进入和真实人员重入事件。
- 人工拔线而非 sysfs 解绑的最终 TTL 与屏幕观察。
- 真实 SMTP 登录、学校邮箱收件、附件、延迟与失败恢复。
- 同一 Wi-Fi/局域网实体手机访问 `8092`、邀请链接、照片与筛选。
- 板屏人工连续画面/闪屏判断、边缘误触、触摸和图标点击。
- 图标迁移、`systemui` 重启与实际整机重启。
- WiFi 目标网络认证、DHCP、保存和失败恢复。
- 物理场景中的 7 天/容量清理效果。

## 6. 发布边界

本报告记录主机软件与板端核心链路。它不声明真实邮件投递、实体手机、人工屏幕
或物理多人事件已完成；这些结果必须来自独立 run，不能用本轮回放、sysfs
解绑或程序化 ACK 填补。
