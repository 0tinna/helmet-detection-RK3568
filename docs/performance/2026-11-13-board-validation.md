# RK3568：SSH 恢复、回归修复与实机模型比较

## 本轮结果

已通过 Windows 原生 OpenSSH 免密连接 `root@192.168.1.3`。此前限制来自受限执行环境；本轮未修改 SSH 配置、密钥或板端登录设置。

板端已有 `/userdata/ai` 和原模型，但没有 `/userdata/helmet`。本轮全部上传和生成文件位于 `/userdata/helmet-validation/20261113`，保留原有实验目录。设备枚举未发现新增 USB 摄像头，当前测试使用静态图片。

## 修复与验证

1. `helmet/helmet_realtime.py`：修复非法 `with…else`，在锁外等待新帧；将 duration 检查移到主循环顶部，避免无帧时无限等待，并让有显示模式也经过同一检查。
2. `helmet/pc/eval_map.py`：保留完整测试图片和 ground truth；缺少预测条目时按空检测列表处理。
3. `helmet/validate_comparison.py`：比较实验必须包含原模型和五个新模型，每项测量为 50 loops，且时延/FPS 为有限正数。失败返回非零状态。
4. `run-board-comparison.sh`：运行后或普通异常退出时恢复 NPU governor，读回确认并留档；HUP/INT/TERM 接入恢复路径。强制断电或 SIGKILL 不经过 shell trap。

采用板端 Python 内置 unittest，依次观察原版失败、最小修改、回归通过。最终 **7 项测试通过，无 SKIP**：CLI 启动；无有效帧时按时退出；缺失预测计入漏检；空检测文件保留测试集；完整比较通过；缺模型拒绝；负时延拒绝。另执行源码 compileall、shell 语法检查和实际结果校验。独立代码审查未发现本轮新增 Critical/Important 问题。

测试命令（在板端验证目录）：

```sh
cd /userdata/helmet-validation/20261113/src
RK3568_TEST_MODEL=/userdata/models/helmet-640-640.rknn python3 -m unittest discover -s tests -v
```

## 第一轮同条件实测

输入为 `/userdata/models/1.jpg`，1280×960；confidence 0.25；每个模型预热 5 次、测量 50 次。所有模型使用相同的当前 `yolo_post.py`，顺序固定。以下 E2E 是预处理、推理和后处理的处理耗时，尚不包含相机采集、显示和报警。

| 模型 | NPU 均值 / ms | 后处理均值 / ms | 处理 E2E 均值 / ms | 等效处理 FPS |
| --- | ---: | ---: | ---: | ---: |
| 原模型 640×640 | 121.92 | 12.61 | 137.83 | 7.3 |
| 新模型 416×416 | 41.92 | 11.79 | 62.82 | 15.9 |
| 新模型 512×288 | 53.86 | 11.87 | 73.15 | 13.7 |
| 新模型 512×512 | 67.86 | 12.35 | 100.82 | 9.9 |
| 新模型 640×384 | 84.35 | 12.39 | 98.04 | 10.2 |
| 新模型 640×640 | 81.78 | 12.85 | 111.50 | 9.0 |

本轮 416×416 的处理耗时比同场原模型降低约 **54.4%**，按均值时延计算的处理吞吐约为 **2.19 倍**。同为 640×640，新模型的 NPU 均值也从 121.92 ms 降到 81.78 ms。以上是在本次输入与运行条件下比较已有模型的结果，不代表本轮重新训练或转换了模型。

实测 runtime 日志确认：原模型使用 Toolkit 1.4.0、target rk3566；五个新模型使用 Toolkit 2.3.2、target rk3568。RKNNLite 为 2.5.0，板端 librknnrt 为 2.3.2，驱动为 0.9.2；Python 3.10.5、OpenCV 4.5.4、NumPy 1.21.2。

测试中 NPU governor 设置为 performance，结束读取频率为 900 MHz；开始温度 46.1°C，结束约 51.3°C。CPU 自动调频没有锁定，也没有采集逐次频率与尾延迟。原始模型、五个新模型的板端 SHA256 与本地清单一致。测试后现场读取 governor 已恢复为 `rknpu_ondemand`。

原始留档：[结果 JSON](2026-11-13-comparison.json)、[环境与模型指纹](2026-11-13-environment.txt)、[benchmark 日志](2026-11-13-benchmark.log)。旧 benchmark 日志最后仍按历史 141 ms 打印 Speedup，本报告采用同场测量 137.83 ms 计算，避免混用历史基线。

加入结果完整性校验与恢复读回之后，按同一配置复跑成功，六项均通过验证：

| 模型 | 第二轮处理 E2E / ms | 第二轮等效处理 FPS |
| --- | ---: | ---: |
| 原模型 640×640 | 137.94 | 7.2 |
| 新模型 416×416 | 64.16 | 15.6 |
| 新模型 512×288 | 73.84 | 13.5 |
| 新模型 512×512 | 101.29 | 9.9 |
| 新模型 640×384 | 97.23 | 10.3 |
| 新模型 640×640 | 108.19 | 9.2 |

第二轮 [结果 JSON](2026-11-13-comparison-run2.json)、[环境记录](2026-11-13-environment-run2.txt) 和 [日志](2026-11-13-benchmark-run2.log) 分别留档；记录包含 `restored_npu_governor=rknpu_ondemand`。两轮相近结果支持将 416×416 列为性能候选，但不是逐帧尾延迟或精度的证据。

最终 [unittest 日志](2026-11-13-unittest.log) 为 7 项通过且无跳过；[验证清单](2026-11-13-validation-manifest.json) 记录本轮源码和输入的 SHA256。先前 `model-manifest.json` 的源码指纹属于修复前清单，模型本身没有更改。

## 固定时长推理检查

对 416×416 模型，以 confidence 0.25 在仅含同一张图片的目录循环推理，运行 10 秒、无显示、NPU 使用恢复后的 ondemand 策略。程序正常结束，无检测线程异常。最后 30 次检测窗口输出：15.1 FPS，NPU 36.1 ms，处理 E2E 63.1 ms。

这些是窗口统计，不是整段会话均值；文件循环不是摄像头帧率。原代码 Dropped 计数仍有口径问题，本轮不将它作为正式丢帧率。实测日志见 `2026-11-13-smoke.log`。

## 开源核对与下一轮

已在线核对 [Rockchip rknn_model_zoo 的 YOLOv5 实现](https://github.com/airockchip/rknn_model_zoo/tree/837db8e992815ff8647eadb709f07067f4bc3a71/examples/yolov5)，上游 commit 为 `837db8e992815ff8647eadb709f07067f4bc3a71`。官方转换流程包含 RGB 归一化、平台选择和 INT8 校准；官方后处理按 objectness × class probability 筛选。实际导出图是否已经包含 sigmoid，必须与当前裁剪原始 Conv 输出的方式区分。

416×416 和 512×288 是后续精度对比的候选，尚未正式选定默认模型。缺少固定独立测试集，不能证明“准确率 ≥85%”；单张图片和 10 秒推理也不能证明摄像头 QoS 或长时间稳定性达标。

下一轮保持模型和阈值不变，优先修复帧与检测结果绑定、线程故障传播及结果过期状态，补充真实采集到输出的时间戳与 p95/p99；再评价五种尺寸对远距离小头部和未戴帽 recall 的影响。现有默认模型及阈值未改动。
