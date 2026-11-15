# 原生 App 可用性收口：Phase 1 报告

## 当前结论

本地 QML 修复、板端全屏、运行中占位文字隐藏、停止后占位恢复、屏幕边缘点击、30 秒实际 DRM 连续帧以及真实 USB 摄像头拔除均已通过。一次 50 秒监视未触发设备移除，该失败尝试仍保留；随后在人工已准备好的独立会话中实际拔线，设备节点、后端状态和物理屏幕同步记录了断流时序。

## 实现

- 窗口使用物理屏幕 `Screen.width × Screen.height` 和 `Window.FullScreen`，常驻唤回使用 `showFullScreen()`。
- 预览使用两个 Image 图层。旧图在新图 `Image.Ready` 且 generation、source_epoch、frameExpires、minimumCaptureAt 等检查通过前保持可见。
- HTTP 返回后，Qt 解码完成前禁止覆盖同一目标图层，避免连续请求写回同一 Image。
- 新图先置顶并与旧图短暂重叠，100 ms 后再撤掉旧图，避免 Qt 合成器在新纹理尚未进入输出时露出底色。
- 运行期间占位文字只由 `!detection.running` 控制；刷新 Timer 为 125 ms。
- 模型、类别顺序、conf 0.35、NMS 0.45、skip 0、TTL 1 秒、Pipeline、后处理均未修改。

## 验证结果

| 验收项 | 结果 | 证据 |
|---|---|---|
| 检测画面连续，无占位文字闪烁 | PASS（当前候选 30 秒） | 300 帧 DRM 视频，预览区低于灰度 40 的空白帧为 0；559 个状态样本均 `valid=true/running=true` |
| 停止后恢复“点击开始检测” | PASS | 停止前 `frame_id=3033`，停止后 `frame_id=3069`、`state=stop`，实际抓屏显示占位文字 |
| 运行中拔掉摄像头，1 秒内消失 | PASS | 450 帧实际屏幕中第 211 帧仍有画面、第 212 帧清空；910 个状态样本捕获设备节点消失及 `valid=false` |
| 720×1280 全屏且边缘不退回桌面 | PASS | DRM 抓图 720×1280；点击 `(719,1275)` 后窗口和三进程仍在 |
| 单元测试回归 | PASS | `python3 -m unittest discover -s tests`：398 tests，OK，12 skipped |

最终 30 秒视频使用 416/responsive 和 `/dev/video10`；状态样本中的唯一模型为 `helmet_416x416.rknn`，`frame_id` 从 1119 到 2087，无倒退。实际屏幕记录缩放为 360×640 便于逐帧检查，抓取的是 DRM 合成输出，不是 Qt `grabToImage`。

人工拔线时，`/dev/video10` 和 `/dev/video11` 从最后存在样本到首个缺失样本仅跨 54 ms；对应后端从 `valid=true / healthy` 变为 `valid=false / running=true / degraded / source read failure`。同窗口的实际屏幕视频在 10 FPS 下只相隔一帧就从实时画面变为空白，远低于 1 秒 TTL；运行状态下的占位文字没有重新出现。

## 失败候选保留

1. 单 Image 候选：12 秒视频第 115 帧出现一次空白。
2. visible 双缓冲候选：`double-buffer-12s.mp4` 中连续出现空白；隐藏图层的纹理没有被可靠保留。
3. opacity/cache 双缓冲初版：30 秒前的诊断录屏出现多个空白序列。日志证明 HTTP 已返回但 Image 尚未 Ready 时会再次覆盖同一目标，且交换时旧层撤除过早。
4. 解码门控加重叠的暂停版：12 秒无空白，但刷新仅约 4.8 次/秒；随后改为旧层被新层覆盖时可预加载，最终 30 秒无空白。

失败的录像和分析图均保留，没有使用后续结果覆盖。

## 文件身份与临时偏差

- 本地改动前 `Main.qml`：`c680484fa88a21cebf244f8a0846ed165dd2489ea38b19c006ed378feeabcced`
- 本地最终 `Main.qml`：`7ed2edc4c37cd5067e1785dc4f06d8c7ecc6d716c9cdfaebf1aa7e92951bb93e`
- `service.py`、`resident.py`、`launch.py` 本地内容未改，详见 `sha-comparison.json`。
- 板端 v2 中 `service.py=3518e266…` 和 `launch.py=8db80c3c…` 与本机发布源码不同；这是本机临时修改，未纳入发布包。
- 板端 v2 没有 `resident.py`，本阶段使用其既有 one-shot `launch.py`。
- `/opt/ui/src/ATK-DLRK3568/apk3.cfg` 前后 SHA 保持 `f64a8e2f751ff777b98a36a13a0ad062e0812f2bd6ebd7914a512b772f507b11`。

## 主要文件

- `phase1-analysis.json`：最终视频、状态、停止和未触发拔线的机器可读记录。
- `final-30s.mp4`：最终实际屏幕连续帧。
- `status-sampler-33s.jsonl`：高频后端状态。
- `stopped-placeholder-final.png`：停止后的实际屏幕。
- `edge-click-final.png`：屏幕右下角点击后的实际屏幕。
- `unplug-status-50s.jsonl`：未触发设备移除的记录。
- `unplug-physical-1/unplug-analysis.json`：真实拔线的设备、后端和屏幕时序。
- `unplug-physical-1/unplug-physical-45s.mp4`：实际拔线后的 450 帧屏幕记录。
- `unplug-physical-1/unplug-transition-contact-sheet.jpg`：从实时画面到安全清空的逐帧对照。
- `sha-comparison.json`：本地和板端 SHA 对照。
