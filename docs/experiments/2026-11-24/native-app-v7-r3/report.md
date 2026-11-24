# native-app-v7-r3 主机与板端核心验收

日期：2026-11-24

状态：`host-pass`；`board-core-pass`。实体手机、真实未戴帽一人/多人/后进入/
重入事件、人工拔线、人工屏幕连续性、`systemui` 重启和整机重启仍为 `not-run`。

本版把 v7-r2 的邮件与邀请鉴权事件页替换为无登录的直接只读 `8092` 看板。页面
同时显示当前告警/检测状态和已落盘的可追溯未戴帽记录。证据分为主机软件证据和
真实 ATK-DLRK3568 板端证据，二者不互相替代。

## 核心结论

- 发布包为 45 个条目、44 个载荷，SHA256
  `553667bec091d565b46557f533b546145d2e0e5ae47af0c03506c140ce8d9df8`。
- 主机两套测试共 529 项通过，跳过 16 项平台能力测试；Python 编译、
  `git diff --check` 与部署脚本 PowerShell AST 通过。
- 全新 v7-r3 root 在开发板 staging 阶段逐文件校验后才原子改名，没有向旧根补文件。
- `8092/`、`/api/status`、`/api/events` 和 `/healthz` 直接可读；无 Cookie、
  无邀请令牌、无 SMTP；旧 `/alerts` 只做 `302 → /`，写请求为 `405`。
- 真实 UVC `/dev/video10` 使用 `helmet_640x384.rknn` 连续运行 180.412 秒，
  帧号 `1005→6317`，服务与 Qt PID 稳定。
- 程序化隐藏 3 秒期间帧号继续增长；Wi‑Fi 页和检测页复用同一 session、Qt PID
  与 UI instance。
- v7-r3 完整退出后进程、端口、控制 socket 和资源锁全部释放，随后由迁移后的
  BusyBox init 入口启动新 session。
- 文档链接修正后的最终 ZIP 又整体替换板端根，45 个载荷逐文件核验通过；最终
  后台 session 为 `9e61e9f4a4844302ab7e9a597f264b9e`，`8091`/`8092` 正常。

## 证据入口

- [主机验证](host-validation.md)
- [主测试树最终日志](regression-host-v7-r3-final.log)
- [legacy 测试树最终日志](regression-legacy-v7-r3-final.log)
- [板端验证](board-validation.md)
- [发布清单](bundle-manifest-v7-r3.json)
- [整包核验](release-verification-v7-r3.json)
- [最终安装收据](installed-release-v7-r3.json)
- [内网看板验收](dashboard-lan-acceptance-v7-r3.json)
- [物理摄像头长跑原始记录](physical-camera-longrun-v7-r3.raw.json)
- [长跑推导分析](physical-camera-longrun-v7-r3.analysis.json)
- [常驻隐藏与唤回](resident-hide-show-v7-r3.json)
- [终端清理](terminal-cleanup-v7-r3.json)
- [图标与 init 迁移](vendor-and-boot-entry-migration-v7-r3.json)
- [BusyBox 生命周期](boot-adapter-lifecycle-v7-r3.json)
- [最终包部署后验收](final-package-postdeploy-v7-r3.json)
- [主机 Edge 实际渲染截图](dashboard-host-edge.png)

长跑原始脚本把“内部接口与公开接口的 `frame_id` 必须逐值相等”设为通过条件。
两次 HTTP 请求之间检测仍在运行，因此该严格条件为 `false`。原始文件原样保留；
推导文件显示告警态 13/13 一致，公开帧号仅滞后 `0–6` 帧，不据此改写原始结果。

## 仍未验证

- 实体手机在真实 Wi‑Fi 上打开 `192.168.1.10:8092` 并加载照片。
- 物理摄像头前的一人、多人、后进入与重入未戴帽事件。
- 人工拔 USB 摄像头后的最终 TTL 画面观察。
- 板屏人工检查连续画面、闪烁、触摸、全屏边缘和图标点击。
- `systemui` 重启、实际整机重启、开机后实体手机再访问。
- Wi‑Fi 目标网络手动识别、认证、DHCP、保存和失败回滚。
- 物理场景中的 7 天、256 MiB、5000 条和磁盘余量清理。
