# native-app-v7-r1 主机候选验证

日期：2026-11-24

状态：`software-candidate`

现场验收：`not-run`

## 1. 候选身份

| 项目 | 值 |
| --- | --- |
| 版本 | `20261124-v7-r1` |
| 计划 tag | `native-app-v7-r1` |
| 部署根 | `/userdata/helmet-validation/20261124-native-app-v7-r1` |
| ZIP | `dist/helmet-native-app-20261124-v7-r1.zip` |
| ZIP 字节 | `39266311` |
| ZIP SHA256 | `2bc344d4ef4b63ca946cdeff4e1e9f1651f1466cfb89cdff5132e2c1f4697956` |
| `code_identity` | `595da82b06d7f9b0de3c6389f22b938de15fedcf236d56d36ae6461627a02bd4` |
| `alert_feature_identity` | `dc93ea7cb358779eaf0efc3a83b83f2dd848d9fdb09b9e64e788d4c3b232a3dc` |
| manifest | `bundle-manifest-v7-r1.json` |

ZIP 共 47 个条目，其中 46 个 payload、1 个内部 `manifest.json`。已核对每个 payload 的 SHA256 和字节数，并确认不存在 `mail.json`、`public-access.key`、`app-state/`、事件照片、运行 token 或 Python 缓存。

## 2. 自动化结果

主测试树：

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

结果：`520` 项通过，`15` 项跳过。跳过项分别要求 Linux 内核/FD 语义、POSIX 符号链接权限、Unix socket 权限或后置 NPU 硬件条件；没有把跳过项写成通过。

`helmet/tests`：

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s helmet/tests -v
```

结果：`25` 项通过，`1` 项因未设置 `RK3568_TEST_MODEL` 跳过真实 NPU 集成测试。

重点覆盖：

- 连续框去重、多人同帧、后进入人员、短漏检、超过 2 秒重入、generation/source epoch 重置。
- JPEG/JSON 原子发布、7 天保留、字节/数量上限、磁盘余量、损坏记录和路径穿越。
- SSL/STARTTLS、5 秒聚合、单附件、三次重试、有界队列、关闭 flush 与状态脱敏。
- 邀请令牌、HttpOnly/SameSite Cookie、分页、日期筛选、媒体鉴权和只读路由边界。
- 帧过期、epoch 变化、停止状态和 JPEG 失败不进入 AlertHub。
- 常驻运行代际切换后，旧 generation 在进入轨迹跟踪器前即被拒绝。
- 事件保留清理在启动时和运行中每小时执行；清理失败以脱敏状态上报且不中断检测。
- 首次关闭超时不会被永久缓存为失败；在确认 SMTP worker 仍存活时可重试关闭并取得真实终态。
- 常驻固定 `--alert-port 8092`，one-shot 固定 `--alert-port 0`，端口失败不回退、不伪报 ready。
- 发布包版本、根路径、五文件 identity、告警功能 identity、模板包含、live secret 排除和确定性 ZIP。

## 3. 现场未验项

以下项目状态均为 `not-run`：

- ATK-DLRK3568 上物理 UVC 摄像头的一人、多人、后进入、持续停留和重入事件。
- 运行中拔摄像头后的 TTL 抑制与不新增事件。
- Qt 回桌面/隐藏期间检测、落盘和 SMTP 队列继续运行。
- 真实学校邮箱 SMTP 登录、收件、附件和延迟。
- 路由器公网 TCP `8092` 到板端 `8092` 的转发。
- 动态公网 IPv4 的有效期、运营商 CGNAT 与手机蜂窝网络访问。
- 180 秒物理长跑、无进程堆积、帧号单调增长、完整退出与端口/worker/锁释放。
- 屏幕人工观察、触摸、图标迁移和 `systemui` 重启。

`ifconfig.me` 返回地址只证明出站访问；它不证明入站 NAT 可达。主机测试中的 fake SMTP、本机 HTTP 和临时文件也不能替代真实板端、真实邮件或外部手机结果。

## 4. 发布状态

候选包已生成，未覆盖 v1-v6 的任何 ZIP、manifest、实验记录或 tag。尚未创建 Git commit、tag、远端发布、板端部署、图标迁移或 `systemui` 重启；这些动作需要后续明确批准和现场证据。

自审阶段发现并修复多个关闭前缺陷：reset 后旧 generation 仍可进入跟踪器、第一次关闭超时被永久缓存、事件清理只在启动时执行、非对象 JSON 可中断索引加载、过宽私密文件权限被接受、SMTP 异常可终止 worker、聚合邮件重复计数同一可见轨道、启动回滚可误报存活 mailer、公网页缺少真实状态接线、状态轮询重复访问公网 IP 端点，以及事件文件删除失败后索引提前丢弃记录、viewer 启动失败未关闭已创建实例。每项均先由失败测试复现，再修复并通过告警集成组与两套完整主机回归。

旧预审候选 SHA256 为 `3fdd0aadc748230a5d0e57bfff0386dc19df676494f94404b42fdfce455d64f3`；现已改名为 `dist/helmet-native-app-20261124-v7-r1-pre-self-review.zip`，对应清单保存在 `superseded/bundle-manifest-v7-r1-pre-self-review.json`。最终自审前候选 SHA256 为 `2711e24233ecfea0b82c28cc8b7141c20bc88aec6e3fa6dbdf5980dfe9653a8c`；现改名为 `dist/helmet-native-app-20261124-v7-r1-pre-final-review.zip`，对应清单保存在 `superseded/bundle-manifest-v7-r1-pre-final-review.json`。文档核对前候选 SHA256 为 `83f5c17957467d5033e5d30e0a452c7ea51e79d82520275a1560deb398ad7241`；现改名为 `dist/helmet-native-app-20261124-v7-r1-pre-doc-review.zip`，对应清单保存在 `superseded/bundle-manifest-v7-r1-pre-doc-review.json`。三者均不用于安装、提交或发布。
