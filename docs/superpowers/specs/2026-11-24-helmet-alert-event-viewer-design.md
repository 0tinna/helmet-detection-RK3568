# 未戴安全帽事件抓拍、邮件告警与公网事件站设计

状态：用户已批准；`20261124-v7-r1` 软件候选已实现，板端现场验收待执行

目标版本：`native-app-v7-r1`

日期：2026-11-24

## 1. 意图

检测运行期间，只要画面中首次确认出现未戴安全帽的头部，就保存一张带检测框的证据照片，并向
`230010044@mail.szpu.edu.cn` 发送简短告警。邮件包含发生时间、未戴帽人数、开发板动态公网 IP、
事件查看链接和一张照片。多个未戴帽人员先后进入画面时，每个新轨迹均产生独立照片和事件记录。

公网页面只展示已保存的历史告警事件，不暴露实时摄像头、控制接口、模型、WiFi 或退出功能。
事件页采用移动端优先的危险/安全主题，强调照片、时间戳和当前事件数量。

## 2. 目标

1. 同一未戴帽轨迹持续存在时只保存一次，避免每帧重复抓拍。
2. 同一画面内有两个未戴帽人员时，任何新轨迹都产生新事件；已有轨迹继续存在不重复保存。
3. 同一轨迹离开后再次进入，经过清除宽限后作为新事件记录。
4. 断流、结果超过 1 秒 TTL、检测停止、启动换代和 source epoch 变化时，不生成新事件。
5. 事件照片和元数据写入持久目录，App 隐藏或恢复时仍能告警。
6. 事件默认保留 7 天，并受总容量上限约束；启动时和定时任务都执行清理。
7. 新事件经 SMTP 邮件通知。邮件失败不影响检测、预览或事件落盘，并有有限重试和状态记录。
8. 公网只提供经过访问密钥保护的事件页面与照片，不提供实时画面。
9. 动态公网 IP 由开发板主动查询；邮件优先使用最新查询结果。
10. 新增功能可独立关闭，关闭后保持 v6-r2 的检测、App、`8091` 和退出行为。

## 3. 非目标

- 不训练、微调、剪枝或蒸馏模型。
- 不改变 `person`/`hat` 类别顺序；`person` 表示未戴帽裸头，`hat` 表示戴帽头部。
- 不改变 `conf=0.35`、`NMS=0.45`、`skip=0`、`TTL=1s`、后处理或报警阈值。
- 不把 `person` 或告警事件表述成人员身份的确定判断。
- 不公开实时 MJPEG、当前帧、WiFi 配置、检测控制或退出控制。
- 不自动在路由器上创建端口映射；NAT 与端口转发仍由部署者配置。
- 不承诺 HTTP 公网访问具备传输加密；生产公网部署应使用受信任的 HTTPS 反向代理或 VPN。

## 4. 方案比较

### 方案 A：板端事件库 + 独立公网事件站 + SMTP

检测实例继续只有一个。统一事件组件接收已经校验的新鲜结果，保存事件，再异步发送邮件并启动
`8092` 只读事件站。照片、元数据、鉴权和通知都由板端拥有，断网时可继续检测和本地保存。

选择该方案。它满足离线主链路、独立公网历史查看、存储清理和新人员继续抓拍，同时不把无鉴权的
实时画面暴露到公网。

### 方案 B：扩展现有 `8091`

实现较少，但当前 `8091` 是无鉴权实时查看端，直接增加公网映射会暴露摄像头，并混淆实时与历史
事件两种访问边界。未采用。

### 方案 C：云端事件中继

板端只上传事件，由外部云服务展示。它能绕过 CGNAT，但增加账号、费用、隐私和外部可用性依赖，
与当前“板端离线检测、设备自行保存证据”的目标不一致。未采用。

## 5. 架构

```text
UVC / replay
    |
    v
Pipeline freshness + YOLO/RKNN + TTL
    |
    +--> existing preview -> native Qt App / 8091 LAN viewer
    |
    +--> ValidatedPacketTap
             |
             v
       NoHatTrackTracker
             |
        new confirmed track?
             |
             v
       AlertEventHub
        |          |
        |          +--> bounded SMTP worker -> 230010044@mail.szpu.edu.cn
        |
        +--> EventStore -> results/helmet-events
        |
        +--> PublicEventServer (0.0.0.0:8092, access key required)
```

`Pipeline` 仍负责 freshest-result、TTL、source epoch、检测和现有报警状态机。新事件逻辑只在
`DetectionController._fresh()` 已通过、预览 JPEG 已成功编码后观察到结果。它不改变原有报警、
音频、App 和 LAN 预览语义。

## 6. 新事件判定

### 6.1 轨迹

新增轻量 IoU 轨迹表，仅跟踪 `wearing=False` 的检测框：

- IoU 阈值默认 `0.30`，与现有头框语义一致。
- 未匹配轨迹在 `2.0s` 内允许短暂漏检，不重复建事件。
- 新轨迹至少连续命中 `2` 帧才确认，削弱单帧误检。
- source epoch、run generation、停止或重新启动时清空全部轨迹。
- 轨迹身份只用于事件去重，不对应真实身份，不用于生物识别。

### 6.2 抓拍

确认新轨迹后，为该次确认保存一张当前 `packet.annotated_bgr` 的 JPEG。一个确认帧中出现多个新轨迹时
只保存一张共享照片，事件元数据列出所有新轨迹和当前全部未戴帽轨迹。

同一轨迹持续停留不会按时间重复拍照。另一名未戴帽人员进入并形成新轨迹时新增照片。第一个轨迹消失
超过 `2.0s` 后再次出现，作为新事件。

抓拍只发生在：

- 检测正在运行且 worker 仍存活；
- `DetectionController._fresh(packet, pipeline)` 为真；
- `metadata.valid` 为真；
- generation 与 source epoch 未变化；
- capture time 仍在现有 TTL 内；
- JPEG 编码成功。

事件保存后，即使当前实时画面随后过期，已确认事件的证据照片仍保留。事件状态明确记录
`captured_wall_time`、`frame_id`、`source_epoch`、`model` 和检测框。

## 7. 事件数据

默认根目录：

```text
<app-root>/results/helmet-events/
  YYYY/MM/DD/
    <event-id>.jpg
    <event-id>.json
```

每张照片配一份 JSON，至少包含：

```json
{
  "schema": 1,
  "event_id": "20261124T142530123Z-ab12cd34",
  "captured_wall_time": "2026-11-24T14:25:30.123+08:00",
  "captured_at": 123.45,
  "frame_id": 9021,
  "source_epoch": 2,
  "generation": "run-uuid",
  "model": "helmet_416x416.rknn",
  "selection": {"source": "/dev/video10", "profile": "responsive"},
  "trigger_track_ids": [4],
  "visible_no_hat_track_ids": [4, 5],
  "count": 2,
  "detections": [
    {
      "track_id": 4,
      "box": [100, 80, 180, 170],
      "score": 0.86,
      "class": "person"
    }
  ],
  "image": "20261124T142530123Z-ab12cd34.jpg",
  "retention_days": 7,
  "public_ip": "203.0.113.10",
  "email": {"state": "pending"}
}
```

JSON 和 JPEG 使用同目录临时文件、`fsync`、`os.replace()` 原子发布。事件 ID 只含数字、字母和
连字符。公开读取接口通过服务端索引映射事件 ID，不直接拼接任意请求路径。

## 8. 保存与清理策略

默认值：

- 保留时间：`7` 天。
- 总容量上限：`256 MiB`。
- 最大事件数量：`5000`。
- 清理周期：`1` 小时，应用启动时立即清理一次。
- 磁盘安全余量：文件系统剩余空间低于 `128 MiB` 时停止新增事件并报告存储故障，不覆盖已有证据。

清理顺序为最早事件优先。超过 7 天、超过总字节上限或事件数量上限时删除对应 JPEG/JSON。
清理失败只影响存储状态，不阻断检测。索引在启动时由有效 JSON 重建；损坏记录跳过并计入状态，
不把损坏记录展示为正常事件。

所有有效文件必须留在 `<app-root>/results/helmet-events` 内。符号链接、路径穿越、越界目录或
非法事件 ID 一律拒绝。

## 9. 邮件告警

### 9.1 配置

新增私有配置文件：

```text
<app-root>/app-state/mail.json
```

文件权限为 `0600`。仓库只提供无凭据的 `mail.example.json`，真实密码或 SMTP 授权码不进入 Git、
日志、状态接口或发布清单。

配置字段：

```json
{
  "enabled": true,
  "smtp_host": "smtp.example.edu.cn",
  "smtp_port": 465,
  "security": "ssl",
  "username": "sender@example.edu.cn",
  "password": "REPLACE_WITH_SMTP_AUTH_CODE",
  "sender": "sender@example.edu.cn",
  "recipient": "230010044@mail.szpu.edu.cn",
  "timeout_seconds": 8,
  "batch_seconds": 5
}
```

`security` 仅允许 `ssl`、`starttls`、`none`。SMTP 连接和发送使用 Python 标准库 `smtplib`。
未配置、认证失败、连接失败和发送超时都记录为安全错误码，不保存服务器响应中的敏感内容。

### 9.2 聚合与重试

- 新事件先落盘，再进入有界通知队列。
- 第一次新事件到达后等待 `5s`，把窗口内的新轨迹事件合并成一封邮件。
- 主题示例：`[RK3568安全帽告警] 发现 2 名未戴安全帽人员`。
- 正文保持简短：发生时间、窗口内人数和事件数、开发板名称、动态公网 IP、查看链接。
- 邮件附第一张事件 JPEG，作为公网暂时不可达时的证据兜底。
- 同一发送失败最多重试 3 次，退避为 30 秒、2 分钟、10 分钟。
- 邮件队列最多保留 8 个待发送批次；溢出时保留最新事件并记录丢批次数量。
- 发送成功、失败和待发送状态写入事件元数据；不在事件站公开 SMTP 地址或用户名。

邮件链路失败不改变检测状态、告警音频、事件照片有效性或 App 运行。

## 10. 动态公网 IP 与查看路径

邮件发送前查询出口公网 IP，默认使用：

```text
https://ifconfig.me/ip
```

查询超时 `3s`。成功后缓存最近一次合法 IPv4/IPv6 地址及查询时间；失败时使用缓存并在邮件和事件站
标注查询时间，超过 30 分钟则显示“公网地址可能已变化”。

`v7-r1` 使用固定监听和邮件链接端口 `8092`，直接采用查询到的公网 IP。查看链接格式：

```text
http://PUBLIC_IP:8092/alerts?access=INVITE_TOKEN
```

访问密钥安装级持久化在 `app-state/public-access.key`，权限 `0600`，长度为至少 256 bit。
`INVITE_TOKEN` 使用密钥和到期时间生成的短时 HMAC，默认 30 天有效。

该链接能否从外网打开仍取决于路由器端口映射。邮件发送成功不等同于公网入口验收通过。

## 11. 公网事件站

默认监听 `0.0.0.0:8092`，可通过参数或配置关闭。只读路由：

- `/`：无数据的安全说明和登录入口。
- `/alerts?access=...`：验证邀请令牌，设置 HttpOnly、SameSite=Strict 的访问 Cookie，然后
  302 到 `/events`，避免后续内部请求继续携带密钥查询参数。
- `/events`：历史事件页面。
- `/api/events?limit=50&before=...&day=YYYY-MM-DD`：分页事件元数据。
- `/events/<event-id>.jpg`：验证 Cookie 后返回照片。
- `/api/status`：检测是否运行、最近事件时间、保留策略、存储和邮件状态。
- `/healthz`：只返回 `ok` 或 `degraded`，不包含敏感数据。

提交型方法、控制接口、实时图片/MJPEG 和未知路由返回 `404`、`405` 或 `403`，不提供通用文件读取。

安全边界：

- 访问 Cookie 使用 HMAC，经常量时间比较。
- 登录失败按客户端地址限速；连续失败触发短时阻断。
- 所有响应设置 `Cache-Control: no-store`、`X-Content-Type-Options: nosniff`、
  `Referrer-Policy: no-referrer`、`X-Frame-Options: DENY` 和严格 CSP。
- HTML 资源全部内联或以静态常量提供，不依赖 CDN、外部字体或浏览器扩展。
- 公开接口不返回私有控制 token、服务端口、WiFi 密码、SMTP 凭据、文件系统绝对路径或异常原文。
- 非本机客户端可读取事件；所有写操作和 App 控制仍只允许既有 loopback 私令牌通道。

传输边界：直接 HTTP 只适合课程演示、受控网络或临时验收。通过真实公网长期运行时，应在
HTTPS 反向代理、WireGuard/Tailscale 等受保护入口后使用，避免照片和访问 Cookie 在公网明文传输。

## 12. 页面设计

页面遵守给定 Vercel Web Interface Guidelines 中与本项目相关的可访问性和性能规则：

- 移动端单列、桌面双列；不用浏览器测量布局，CSS Grid/Flex 自适应。
- 背景使用近黑海军蓝，安全状态用青色，危险事件用红橙色，少量扫描线装饰。
- 标题显示“现场安全事件”和当前状态；危险不等于人员身份判断。
- 最新事件为大卡片，包含照片、日期时间、累计未戴帽人数、事件编号和“详情”按钮。
- 后续事件按时间分组；图片设置宽高、`loading="lazy"`，大列表限制在每页 50 条。
- 空状态明确显示“尚未发现未戴安全帽事件”。
- 存储不足、邮件失败、检测停止和公网地址过期作为状态提示显示。
- 文字容器处理长内容；时间和数字使用 `Intl.DateTimeFormat`/`Intl.NumberFormat`。
- 所有按钮具备可见 `:focus-visible`、触控目标至少 44 px，图标按钮有 `aria-label`。
- 动画仅使用 `transform`/`opacity`，支持 `prefers-reduced-motion`。
- 页面状态尽量映射到 URL 的日期筛选和分页参数，支持刷新、后退和链接分享。
- 不把实时画面作为公网页面背景或接口回退。

首屏草图：

```text
┌────────────────────────────────────┐
│ RK3568 / SAFETY GRID      LIVE ●   │
│ 现场安全事件                       │
│ 今日 3 次 · 最近 14:25:30          │
├────────────────────────────────────┤
│ [ 未戴安全帽照片 ]                 │
│ DANGER // 2 人                      │
│ 2026-11-24 14:25:30 Asia/Shanghai   │
│ 803 号现场 · 事件 #...              │
├────────────────────────────────────┤
│ 今日时间线                          │
│ 14:25  DANGER  2 人   [缩略图]      │
│ 13:58  DANGER  1 人   [缩略图]      │
└────────────────────────────────────┘
```

## 13. 组件边界

新增文件：

- `helmet/app/alert_events.py`：轨迹去重、事件候选、事件索引和清理。
- `helmet/app/alert_mail.py`：动态公网 IP 查询、SMTP 配置、批次与重试。
- `helmet/app/alert_viewer.py`：`8092` HTTP 服务、访问密钥和页面。
- `helmet/app/alert_hub.py`：组合事件存储、通知 worker 和事件站生命周期。
- `helmet/app/mail.example.json`：无凭据示例。

现有文件的闭合修改：

- `service.py`：创建/关闭 AlertHub，把 hub 注入 `DetectionController`；新增 `--alert-port`。
- `DetectionController.on_packet()`：仅在既有 freshness/JPEG 成功边界后调用
  `hub.observe(packet, jpeg, generation)`；不写阻塞式网络或磁盘操作。
- `resident.py`：服务子进程转发 `--alert-port 8092`，服务关闭收据包含事件站真实关闭状态。
- `launch.py`：one-shot 验证模式默认关闭事件公网监听，避免测试实例占用端口。
- 新 v7 发布 builder：整体打包新增模块和无凭据示例。
- App 生命周期、QML、模型、Pipeline 后处理和 8091 行为保持原有语义。

保持既有五文件 `code_identity`。另增加 `alert_feature_identity`，覆盖事件、邮件、查看器和配置模板，
防止手工拼接新模块。

## 14. 失败处理

| 失败 | 行为 |
| --- | --- |
| SMTP 未配置 | 照片照常保存，事件状态 `not_configured` |
| SMTP 发送失败 | 有界重试；检测和预览继续 |
| 公网 IP 查询失败 | 使用缓存或标记未知；不阻止邮件 |
| 路由器未映射 `8092` | 邮件仍发送，但链接不可达；状态注明待公网验收 |
| 事件磁盘写失败 | 不展示伪造事件；记录存储降级；检测继续 |
| 磁盘低于安全余量 | 停止新增事件，保留现有证据，页面提示 |
| 事件 JSON 损坏 | 启动索引跳过并计数，不公开损坏文件 |
| 端口 8092 被占用 | 服务新实例启动失败并给出真实失败；不再禁用后声称可用 |
| 检测断流或 TTL 过期 | 不生成新事件，不补写旧帧 |
| App 隐藏 | 检测 worker 继续，事件和邮件继续 |
| 完整退出 | 停止 SMTP/清理/HTTP worker 并确认端口实际释放 |

## 15. 测试与验收

### 15.1 自动化

- 轨迹：连续同框只一次；同时两人一次照片两条轨迹；后续第二人新增事件；离场超过宽限后重新进入
  新增事件；短暂漏检不重复。
- freshness：停止、切 source、source epoch 变化、TTL 过期、fault 时不产生事件。
- 存储：JSON/JPEG 原子发布、非法 ID/路径拒绝、7 天清理、字节和数量上限、损坏记录跳过。
- 邮件：使用 fake SMTP 验证 SSL/STARTTLS、脱敏错误、正文、附件、批次聚合、3 次重试和队列上限。
- 公网查看器：无密钥 `403`；邀请链接设置 HttpOnly Cookie；事件分页/图片/日期筛选；非法路径拒绝；
  无实时/控制路由；安全响应头；并发客户端上限；关闭后端口释放。
- 浏览器脚本：Node 边界测试时间格式、分页、空状态、过期地址提示和失败重试。
- 回归：两套 unittest 全部通过；现有 App 生命周期、`8091`、双缓冲、TTL 和退出测试不变。

### 15.2 板端实测

1. 全新 v7 根整体部署，不在旧根上逐文件覆盖。
2. 戴帽人员持续经过，不生成危险事件。
3. 一人未戴帽进入：只出现一张证据照片，并收到一封简短邮件。
4. 同一人持续停留：事件数不增长。
5. 第二、第三名未戴帽人员进入：分别新增照片；5 秒窗口内的邮件合并但事件记录不丢。
6. 人员离场超过 2 秒后再次进入：新增事件。
7. 运行中拔掉摄像头：不新增事件，不把旧帧写成新证据。
8. 关闭板端窗口/回桌面：检测帧号继续，新事件仍落盘和发邮件。
9. 使用手机蜂窝网络（关闭 WiFi）访问邮件链接，验证公网入口、鉴权、照片和分页。
10. 人工检查邮件正文和照片，确认收件地址和动态公网 IP 正确。
11. 连续运行至少 180 秒，确认无进程堆积、无重复爆炸、存储增长有界。
12. 退出后确认 `8091`、`8092`、SMTP worker、检测 worker 和 socket 全部释放。

真实 SMTP 凭据、路由器端口映射和外部蜂窝网络测试不在自动化测试中伪造。若任一项未完成，报告
必须明确列为未验项。

## 16. 发布

- 新 tag：`native-app-v7-r1`。
- 新部署根：`/userdata/helmet-validation/20261124-native-app-v7-r1`。
- 保留 v6-r2 ZIP、manifest、tag 和实验记录。
- 发布清单记录完整 ZIP SHA、既有五文件 `code_identity`、`alert_feature_identity` 和新增文件 SHA。
- `mail.json` 与 `public-access.key` 是板端私有配置，不进入 ZIP 或 Git。
- 文档补充 SMTP 配置、路由器端口映射、动态 IP、访问密钥轮换、证书/反向代理和清理策略。
