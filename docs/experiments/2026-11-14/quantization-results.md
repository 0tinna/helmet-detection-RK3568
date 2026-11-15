# 同源 416×416 FP16 / INT8 量化对照：实际结果与独立审计

本轮完成同源模型构建、实际 RK3568 推理速度和两套固定诊断子集的逐类质量对照。INT8 相比 FP16，平均推理 API 时间从 101.17 ms 降至 41.87 ms（2.42 倍）；图像处理 E2E 从 118.49 ms 降至 64.43 ms（1.84 倍）。SHWD / HardHat 的两类平均项目 AP50 分别下降 0.85 / 1.12 个百分点。此结论限定于本轮固定模型、子集和条件，不能宣布 85% 质量目标或物理摄像头、显示、长时稳定性验收通过。

## 同源构建与校准

这是新构建 FP16 与新构建 INT8 的成对实验，未混用历史六个 INT8 模型。源 ONNX SHA256 `7ea304dbfc05a549c3f0c10890a5d20fc9bbd4d34df2680265d78316e6b770a7`，上游提交 `38eb6541ff71c3cf7160b27a16c2d8d2742500b1`。两者均用 Toolkit2 2.3.2、`images` `[1,3,416,416]`、Conv 输出 `350/364/378`、mean=0/std=255、RK3568、optimization=3；INT8 的 normal/channel 设置与 FP16 配置相同，量化开关与校准输入不同。配置和导出文件均重新核验。

| 模型 | 文件大小（十进制 MB） | SHA256 | 构建耗时 |
|---|---:|---|---:|
| FP16 | 15.411133 | `517fb9113337436f89a5129e62beb73156a37122bc1ffac57fd3349600b1ad45` | 148.7 s |
| INT8 | 8.110065 | `c93359dc2b13370b3e5619cf6c27826ccbc6056850dbb8f963cc523996239195` | 5286.0 s |

INT8 使用原 SHWD `train.txt` 顺序前 100 张图，只做校准，没有训练或微调。原图、预处理图、源拆分、列表和有序文件身份哈希全部验证；有序身份 SHA256 `bf888bc1d9d6b31c524798614b0132fe34086de76808d9bd59aec942a6f3286d`。按已有转换器做 416 letterbox、INTER_LINEAR、114 填充、JPEG 95。重新对比全部 400 张评估输入，训练侧 ID 与 SHWD 固定 test ID 无交集，字节/解码像素重复为 0，dHash 距离≤6候选为 0。这是有限输入检查，不能证明模型训练独立性。

便携 Ubuntu 22.04 x86 QEMU 环境保留了两次 WHPX CPU 指令失败记录；Toolkit 原生 `vxorps` 需要 AVX，而该 WHPX guest 未暴露 AVX/XSAVE。换用 TCG `max` 后实际 CPUID 满足要求，两模型成功导出。构建进程 2 vCPU、2 GB RAM、4 GB swap，OMP/OpenBLAS/MKL/NumExpr 线程上限为 1。该环境说明转换可复现，不构成 CPU 量化推理测试。

## 实际板端速度与大小

| 指标 | FP16 | INT8 | INT8 相对变化 |
|---|---:|---:|---:|
| 推理 API 均值 / ms | 101.17 | 41.87 | -58.61% |
| 后处理均值 / ms | 11.11 | 11.46 | +3.15% |
| 图像 E2E 均值 / ms | 118.49 | 64.43 | -45.62% |
| 由 E2E 均值换算吞吐 / FPS | 8.44 | 15.52 | 1.84 倍 |
| 结束时温度 / °C | 51.9 | 53.8 | 顺序升温，未锁定 |

RKNN 文件从 15.41 MB 降至 8.11 MB，减少 47.38%。

![实际平均时间与模型大小](quantization-figures/performance-size.png)

实际运行 `quantization-pair-1` 在 2026-11-14 09:06:14–09:09:13 UTC（17:06–17:09 北京时间）。两者日志都记录同路径 `input/1.jpg`、1280×960、100 loops、10 warmups；benchmark 代码显示推理、后处理和 E2E 三阶段各 100 次，预热为推理 10 次。FP16 先测，随后进行其两来源 dump，再测 INT8；不是随机交叉顺序。推理均值测 RKNN API；E2E 包含 resize/pad、颜色转换、推理和后处理，不含摄像头采集、显示、音频或编码。FPS 是 E2E 均值的倒数，不是显示 FPS。

日志证实板端 RKNN-Toolkit-Lite2 **2.5.0**、librknnrt **2.3.2**、driver **0.9.2**、模型 compiler/toolkit **2.3.2**。静态 shape 的 dynamic-range query warning 已记录，模型实际推理与全部 dump 成功，不将 warning 改写成失败。NPU governor 写入 `performance` 的日志存在，原 `rknpu_ondemand` 在 finally 中恢复且读回一致；本轮未保存中间 performance/frequency 读回，未锁定 CPU 频率或温度。两个温度读数仅为各 benchmark 结束值。没有逐 loop 原始时间、p95/p99、置信区间或跨会话重复证据，故不声明尾延迟收益或固定热条件性能。

benchmark 运行清单未记录采样图的运行时 SHA256。两日志相同路径和尺寸支持同图条件，主机 `models/1.jpg` SHA256 为 `a672e54b698710b9ff3081273fced9b32c671976203f568a3ef505dce1c5936e`。已保存 `post-run-input-audit.json`：板端相同路径事后读回 176,224 bytes，SHA 与主机一致，scope 明确标注 post-run、非 contemporaneous stamp；这是事后身份核验，不能升级为运行当时的字节时间戳。其 governor 读回 ondemand 也仅证明事后状态，不证明中间 performance 频率。

## 两来源逐类质量与百分点差异

AP 为 IoU=0.5、保留 score≥.001 的项目 all-point AP50；P/R/F1 在 confidence≥.25，NMS IoU=.45，最多 300 detections。未计算官方 COCO AP50:95。两来源各 200 张原预设输入完整保留，两精度模式的文件名、ID、维度、SHA、GT 元数据及 decoder 配置完全一致，四次均 200/200 成功、0 errors。

| 来源 | FP16 mAP50 | INT8 mAP50 | INT8 − FP16 / pp |
|---|---:|---:|---:|
| SHWD，200图 | 71.88% | 71.03% | -0.85 |
| HARDHAT，200图 | 79.66% | 78.55% | -1.12 |

| 来源 / 类（GT 数） | FP16 AP50 / P / R / F1（%） | INT8 AP50 / P / R / F1（%） | INT8 − FP16 AP / P / R / F1（pp） |
|---|---|---|---|
| SHWD person（212） | 62.36 / 65.05 / 63.21 / 64.11 | 61.80 / 59.83 / 64.62 / 62.13 | -0.55 / -5.22 / +1.42 / -1.98 |
| SHWD hat（786） | 81.41 / 82.66 / 80.66 / 81.65 | 80.26 / 78.35 / 78.75 / 78.55 | -1.14 / -4.31 / -1.91 / -3.10 |
| HARDHAT person（262） | 81.67 / 72.58 / 82.82 / 77.36 | 80.70 / 69.33 / 82.82 / 75.48 | -0.97 / -3.25 / +0.00 / -1.88 |
| HARDHAT hat（694） | 77.66 / 89.68 / 72.62 / 80.25 | 76.40 / 85.25 / 71.61 / 77.84 | -1.26 / -4.43 / -1.01 / -2.42 |

![逐类 AP 与运行阈值质量](quantization-figures/class-quality.png)

![INT8 减 FP16 的百分点变化](quantization-figures/quality-deltas.png)

运行阈值的变化比 AP 均值更明显：SHWD 裸头召回提高 1.42 pp，但误检从 72 增至 92，precision 下降 5.22 pp；SHWD 安全帽 TP 从 634 降至 619、FP 从 133 增至 171。HardHat 裸头 TP 同为 217，FP 从 82 增至 96；安全帽 TP 从 504 降至 497，FP 从 58 增至 86。因此本轮 INT8 的更低平均时间伴随各类 precision/F1 下降，不能只用约 1 pp 的平均 AP 下降概括运行阈值代价。

`person` 始终表示裸头，`hat` 表示戴安全帽的头部。SHWD 212 裸头 / 786 安全帽 GT；139 个 difficult hats 按既定诊断策略作为普通 GT，不等同标准 VOC difficult-ignore。HardHat 262 裸头 / 694 安全帽 GT，原 35 个全身 person 明确在规范化时忽略，绝不映射成裸头。所有面积 AP 与运行阈值 TP/FP/FN 均用当前 evaluator 和实际历史 snapshot 重新计算，数值在 1e-12 容差内吻合。

规范 SHWD 输入的七个原 XML 文件名解析映射和原始哈希保持不变。共享后处理在图像边界裁剪后产生的 finite zero-area 候选按既定带 trace 的策略删除；SHWD FP16/INT8 各 7 个，HardHat 各 0 个。原 trace 与 counts 保留，未把失败推理处理为零检测。

两套子集训练 overlap 未知，存在一张已确认共享原照片；原来源没有 scene ID，每套固定样本保留其 unknown-scene group。不把随机文件名或无字节重复解释为场景独立，也不给本轮 200 图结果加上完整数据集或认证独立测试标签。逐类变化和 2 类均值分别报告；没有对这两套诊断集调阈值，没有统计显著性或 85% 目标通过的结论。

## 证据核验与复现

实际运行 manifest 的五个源码 SHA（benchmark、dump、evaluator、dataset_tools、decoder）与 `quantization-pair-1/runtime-snapshot/` 全部一致。当前 evaluator/decoder 与历史哈希有差异，报告保留历史身份；重新评估同一 dump 后类/面积 AP、P/R/F1 与计数一致。原模型、raw dump、raw metrics、来源和手册/计划没有被本报告脚本修改。

```powershell
.\.venv\Scripts\python.exe docs/experiments/2026-11-14/build_quantization_report.py
```

脚本只读原模型、构建清单、100 校准输入、400 规范评估输入及本轮 raw artifacts，写本报告和 `quantization-figures/`。其中 `quantization-verification.json` 保存每项检查和校准 overlap 重算；`*.current.metrics.json` / `*.recorded.metrics.json` 保存重算值，PNG 和 PDF 是独立图件。

这支持 B1 同尺寸 FP16 构建、B2 同图短时平均 infer/E2E 比较、B3 同固定集合逐类 AP 对照的实际实验完成；计划勾选由协调 owner 依据这些证据执行。进一步固定热条件/CPU 频率/随机顺序/尾分布实验、独立有 scene provenance 的质量评估和真实摄像头最终型号选择仍是不同验收。
