# App LAN Preview And Boot Startup Implementation Plan

> **For agentic workers:** Use `superpowers:executing-plans`. 用户已指定由主代理完成；不派发子代理。按已授权的配置请求连续执行。

**Goal:** 开机后台启动及图标打开后，手机可通过内网 `http://板端IP:8091/` 查看同一检测实例。

**Architecture:** 新增只读 LAN HTTP 适配器，复用控制器的校验 JPEG；后台入口复用现有 resident/socket/lease。BusyBox init 只负责有界正常启动和确认退出。

**Tech Stack:** Python 标准库 HTTP/threading、现有 OpenCV JPEG、Qt/QML、Node 浏览器脚本回归、BusyBox init。

**Spec:** [App 内网与开机设计](../specs/2026-11-15-app-lan-autostart.md)。

## Global Constraints

- 端口 `8091`，私有控制继续 loopback/令牌校验；公共接口只读且不包含 WiFi/私有会话资料。
- 五文件 code_identity、POSIX 独立 session、所有者/资源锁、正常退出证据保留。
- 不改 Pipeline/后处理/模型，person=裸头/hat=戴帽头部、conf .35/NMS .45/skip0/TTL1s 固定。
- 不覆盖 v1–v4-r2 包、manifest、历史 SHA 和 run；使用新完整 root。
- 主代理执行；沿用当前工作区与独立功能分支，使用现有主机依赖，不重启厂商桌面脚本。

## Review Focus

- 手机连接挂起/解码回调迟到时仍必须到期隐藏旧帧。
- 短暂源失效后恢复、新 generation 同帧号不能展示旧 JPEG。
- LAN 端口冲突必须可见失败，不偷换端口或留下半启动资源。
- boot 重复 start 不得抢窗口、重启检测或堆积实例。
- stop 受理但进程/锁尚未释放不得报告成功；失败清理也不得当作成功退出。

### Task 1: Shared Read-only LAN Preview

**Files:** 新增 `helmet/app/lan.py`、`tests/test_app_lan.py` 与网页脚本回归；修改 `helmet/app/service.py`。

**Interfaces:** `LanPreview(controller, host='0.0.0.0', port=8091, max_clients=4)` 提供 `start()/close()/snapshot()`；`LocalService(..., lan_port=None)` 在同一服务生命周期管理它，服务 CLI 默认 LAN8091/0禁用。`/api/preview` 返回同源 JSON 包与 JPEG base64，`/status.json` 为检测状态，`/frame.jpg` 与 `/stream.mjpg` 保留帧身份。

- [ ] 先写并运行失败回归：无令牌公共只读、私有403、同 JPEG/帧号、TTL/epoch/stop、端口冲突、退出释放与流上限。
- [ ] 实现只读 HTTP 与有界手机页计时，接入 LocalService；保持已有输出回调和 Qt 不变。
- [ ] 执行 Node 网页脚本：迟到解码不恢复、挂起请求到期、请求耗时扣除、新代次隐藏；再运行 App 相关回归。

### Task 2: Background Bootstrap And Confirmed Init Shutdown

**Files:** 修改 `helmet/app/launch.py`、`helmet/app/resident.py`；新增 `helmet/app/boot.py`、`helmet/app/autostart.sh` 与对应测试。

**Interfaces:** `launch.py --background` 仅确保后台存在；`resident.client(..., background=False)` 启动后台时发 status，不覆盖活实例意图。`boot.py start|stop|status --root ROOT` 提供有界 LAN ready/真实退出确认；init 脚本解析自身全路径找新根。

- [ ] 先验证后台创建的失败测试，复用原 Linux fixture，确保后来 show 同 session。
- [ ] 实现后台启动意图与按需 Qt；原 show/hide/quit 控制不变。
- [ ] 先写退出受理/活 owner/失败收据不能成功的回归，再实现 boot 等待；运行 shell 语法与真实 fixture。
- [ ] 主树与 legacy 树回归，明确平台/NPU skip。

### Task 3: Complete Release, Board Validation And Save

**Files:** 新 v5 整包、`docs/experiments/2026-11-15/app-lan-autostart/` 新报告/清单、当前 App/交付资料与 GitHub 保存规则。

- [ ] 记录旧/新代码 SHA 与版本，完整构建 v5 包，逐项核验；整体部署至新 root。
- [ ] 以一次检测实例验证内网状态/JPEG/浏览器、隐藏/复开/双入口、停止旧帧和完整退出，180 秒相机短跑。
- [ ] 在终态备份旧入口/cfg/desktop，迁移项目自有入口和 init 链接，验证正常两图标启动。
- [ ] init start/重复 start/stop 证据；保存所有记录后检查一次重启的实际后台/端口/同实例图标接入。
- [ ] 报告失败与未验，更新当前资料/HTML；验证后提交并同步 GitHub main。
