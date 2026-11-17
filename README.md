# 基于 RK3568 的离线安全帽检测与状态闭环系统

**当前交付版本：v6-r2，版本 tag：`native-app-v6-r2`。** 本项目使用现成社区 YOLOv5 模型，在 ATK-DLRK3568 上完成本地安全帽头部检测、Qt 全屏、后台常驻、原帧时效校验和可追溯评价。v6-r2 在 v6-r1 的精确二进制预览、双 Image、`8091` 内网查看和 BusyBox 开机入口上，修复回桌面后再次点图标闪退，并让空闲页自动刷新输入源。

日常检测使用板端模型与 USB 摄像头，不需要开发板浏览器、电脑页面或持续联网。项目范围不包含训练、微调、剪枝或蒸馏。

手机连接同一可互通内网后，访问 **`http://192.168.1.3:8091/`**（按当前 IP 调整）。开机入口配置后台待机，图标打开后手动检测；回桌面保留内网查看，完整退出关闭监听。v6-r2 完成窗口可见性冲突修复、输入源自动刷新、五轮程序化隐藏/唤回、双图标复用和完整退出；预览性能沿用 v6-r1 的可见态检测约 7.80 FPS、呈现约 4.40 FPS、Qt CPU 33.25%。物理摄像头热插拔、板屏人工连续性和实体手机仍按[v6-r2 报告](docs/experiments/2026-11-17/native-app-v6-r2/report.md)保留未验。

## 从哪里开始

| 目的 | 当前资料 |
| --- | --- |
| 了解交付内容、版本和已知限制 | [项目交付说明](docs/项目交付说明.md) |
| 初次了解项目的阅读顺序 | [阅读导航](docs/阅读导航.md) |
| 查看系统蓝图、算法契约、部署、CLI 和评估 | [项目蓝图与完整手册](docs/项目蓝图与完整手册.md) |
| 在板屏操作检测、常驻和 WiFi | [App 操作资料](docs/app/README.md) |
| 手机访问、开机入口与版本回退 | [手机及 init 使用指南](docs/app/手机内网查看与开机启动-v6.md)、[Git 版本管理](docs/版本管理.md) |
| 准备开题报告、论文与演讲 | [学术材料目录](docs/proposal/README.md) |
| 直接阅读或演示 | [离线 HTML 手册](docs/deliverables/项目手册.html)、[HTML 演示](docs/deliverables/开题演示.html)、[可编辑 v7 PPT 底稿](docs/deliverables/开题报告-RK3568-v7.pptx) |
| 核对真实实验和验收收据 | [实验报告索引](docs/experiments/README.md) |
| 查找过期初步文档、原开题与旧 PPT | [历史资料归档](docs/archive/README.md) |

全部资料的目录职责见 [docs/README.md](docs/README.md)。

## 已交付的系统

- Qt Quick 板端原生 App：720×1280 全屏、双 Image 连续预览、模型/输入选择、开始/停止和报警截图。
- 常驻生命周期：回到桌面继续检测，再点任一图标复用同一会话，两个入口只切换检测/WiFi 页面。
- 内网查看与启动：只读 `8091` 页面共用现有 JPEG/NPU，BusyBox `S99zhelmet-app` 配置后台待机，重复启动复用实例。
- 原帧与状态闭环：帧 ID、采集时间、generation/source epoch 和 1 秒 TTL 约束预览、报警与保存；停止和断流及时抑制旧结果。
- WiFi 操作界面：状态、扫描、SSID 选择、屏幕密码键盘和手动连接入口。
- 共用 CLI 与评价工具：JSONL/会话/资源记录、可选 Web/事件录像、六模型诊断及同源 FP16/INT8 对照。

类别顺序固定 `('person', 'hat')`，其中 `person` 表示裸头，`hat` 表示戴帽头部。App 固定 conf `0.35`、NMS `0.45`、skip `0`、TTL `1 s`。无检测或检测不可用不能保证全员戴帽。

## 当前运行包与验证

| 项目 | 当前值 |
| --- | --- |
| 运行包 | [helmet-native-app-20261117-v6-r2.zip](dist/helmet-native-app-20261117-v6-r2.zip) |
| 发布清单 | [bundle-manifest-v6-r2.json](docs/experiments/2026-11-17/native-app-v6-r2/bundle-manifest-v6-r2.json) |
| ZIP | 39,236,091 字节，41 entries（40 payload 与内部 manifest） |
| ZIP SHA256 | `e66dc2983ca28258f28507c16a1be0b3aea637136166fce8c0c125b2a474c2bc` |
| 板端安装根 | `/userdata/helmet-validation/20261117-native-app-v6-r2` |
| 正式入口 | `/opt/ui/src/apps/helmet_app`、`/opt/ui/src/apps/helmet_wifi` |
| 开机入口 / tag | `/etc/init.d/S99zhelmet-app` / `native-app-v6-r2` |

[v6-r2 可用性修复报告](docs/experiments/2026-11-17/native-app-v6-r2/report.md)记录回桌面复开闪退修复、输入源自动刷新、五轮程序化隐藏/唤回、双图标复用和完整退出：主树 440 项通过（15 skipped）、legacy 25 项通过（1 skipped）；本次回放帧号 `51→132→212→264→345→377`，`session`、UI instance 与 Qt PID 均保持不变。当前板端没有 USB UVC 节点，因此真实热插拔仍待现场验证。

[v6-r1 性能报告](docs/experiments/2026-11-17/performance-baseline/report.md)继续作为 v6-r2 沿用的性能基础：Qt CPU 从 56.50% 降至 33.25%，预览总载荷降低 56.25%，180 秒采样约 8.08 检测 FPS、约 4.41 呈现 FPS，帧号 `39→3127`。该成绩属于 v6-r1，不能改记为 v6-r2 新测量。

历史 [v5-r3 报告](docs/experiments/2026-11-15/app-lan-autostart/report.md)保留 180.50 秒真实 USB 相机与 8091/init 证据；[Phase 1](docs/experiments/2026-11-15/native-app-usability-v4-phase1-1/report.md)保留全屏与物理拔线 TTL，[Phase 2](docs/experiments/2026-11-15/native-app-usability-v4-phase2-1/report.md)保留 v4-r2 常驻/196.67 秒相机短跑。它们各有自己的版本与计时范围。

历史 30 分钟回放、相机短跑和诊断精度各有独立运行条件。详细计数、模型/代码身份及失败记录请按[实验索引](docs/experiments/README.md)进入对应报告。

## 使用边界

OS、Qt/Wayland、Python、OpenCV、NumPy、RKNNLite 和驱动需预装。当前实现应用级离线运行，已验证的更新方式是完整包复制到新根；U盘安装、无线 OTA 和无人值守更新未实测。

WiFi 扫描与输入界面已有证据，真实认证、DHCP、保存与失败恢复仍未实测。`systemui` 重载后的实体图标手指操作、独立供电、长期现场、持续显示/内存稳定性、声音与业务事件质量保留为交付限制。

两套各 200 图数据仅作诊断：训练重叠未知且有一张共有照片，最高诊断 mAP50 低于 0.85。独立质量目标和最终模型资格未通过。全部限制集中在[项目交付说明](docs/项目交付说明.md)，原始报告不补写通过。

## 目录与代码入口

```text
helmet/app/            Qt App、本机/LAN 服务、WiFi、resident 与 BusyBox init
helmet/                CLI、Pipeline、源、报警、输出、遥测与共享后处理
helmet/pc/             转换、数据规范化、精度和错误分析
helmet/models/         既有输入配置及成对量化模型
models/                原模型、样图与来源 ONNX
tests/                主回归树
helmet/tests/          既有 CLI/输出/比较回归
datasets/             数据来源、规范标注和本地原始素材
docs/                 当前交付资料、操作指南、实验与历史归档
dist/                 当前运行 ZIP 与本地历史发布载荷
```

板端操作从 [App 手册](docs/app/板端App使用手册.md)开始；命令行部署和复现见[完整手册](docs/项目蓝图与完整手册.md)。SSH 别名为 `rk`，串口工具在 `.codebuddy/tools/`，作为 SSH 不可用时的兜底。

主机已有依赖环境时，可运行软件回归：

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe -m unittest discover -s helmet/tests -v
```

NPU gated 用例需要真实板端模型/运行环境；主机软件测试不代替相机或现场验收。`tools_ai/ref/test.py` 是上游参考脚本。

## GitHub 保存范围

GitHub 保存源码、测试、模型、v6-r2 当前包、v6-r1 历史正确包与 wrapper 拒绝包、已归档 v5-r3/v4-r2 ZIP、Markdown/HTML/PPTX、实验清单及关键收据；正式版本以带说明的 tag 保存。`.runtime/`、`.venv/`、`.tmp/`、`.superpowers/`、`datasets/raw/`、外部数据图片、大视频/JSONL、其余重复候选 ZIP 和个人模板保留本机。

历史报告中的本地引用代表原证据位置，克隆仓库时可能没有完整载荷。过期原稿与旧 PPT 的位置和 SHA 见[归档清单](docs/archive/archive-manifest.json)；本次整理说明见[资料整理记录](docs/资料整理记录.md)。
