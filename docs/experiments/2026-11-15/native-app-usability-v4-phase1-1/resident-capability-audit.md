# resident.py 能力审计

1. 控制协议支持 `status`、`show`、`hide`、`quit`，Qt 回执使用 `ack`，验证截图使用 `render`。
2. 图标客户端只处理一次控制请求；独立的 resident manager 通过 `start_new_session=True` 持有 service 与 Qt。
3. `show` 更新目标可见性、页签和最小预览捕获时间；Qt ACK 报告实际窗口可见性和页签。
4. 隐藏窗口后停止预览请求与解码，状态轮询和后端检测继续。
5. 再次 `show` 复用同一 manager、service 和 session；Qt 若已退出，manager 可按原会话重启 Qt。
6. `quit` 修改停止意图并等待 service/Qt 实际退出、资源释放和私有文件清理，受理本身不算成功。
7. owner record 校验 root、版本、session、manager/service/UI 进程身份与控制 socket。
8. PID 身份由 PID 与 `/proc/<pid>/stat` start ticks 共同构成，避免 PID 复用误判。
9. Unix socket 请求使用同 UID 校验和 `SO_PEERCRED`，客户端还会核对 manager argv 与 root。
10. `code_identity` 覆盖 `resident.py`、`launch.py`、`service.py`、`wifi.py`、`qml/Main.qml` 五文件。
11. 资源 flock 在 service 启动前继承；manager 丢失时 service 仍持有同一 open description。
12. 启动/管理/资源锁分别使用 `startup.lock`、`manager.lock`、`resource.lock`。
13. 私有 token、ready、service/Qt 日志、回执和 QML wrapper 位于 `app-state/session-*`。
14. 当前实现保留完整身份与 lease 校验；本轮不依赖独立 review、1800 秒 gate 或离线网络命名空间作为验收替代。
15. Phase 2 只验证 show/hide/quit、同 session 复用、帧号连续增长、实际清理与 180 秒短跑，并如实列出降级范围。
