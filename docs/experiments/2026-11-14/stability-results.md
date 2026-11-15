# 当前源码30分钟真实NPU回放：稳定性与资源结果

2026-11-14，`longrun-final`在RK3568上完成当前流水线/遥测/检测头源码的30分钟录像回放。**运行、逐帧连续性、计数守恒、配置绑定和资源覆盖校验通过；检测9.772426970 FPS、输出9.771871434 FPS，均低于10 FPS。** 该结论不能写成摄像头/屏显/手机演示、零内存增长或全部性能目标通过。

本报告只读取已有实验并重新核算，未改运行源码、模型、原始manifest/log、阈值或板端状态。依据：[运行manifest](longrun-final/manifest.json)、[strict verification](longrun-final/verification.json)、[session](longrun-final/session.json)、[逐帧记录](longrun-final/frames.jsonl)、[独立资源记录](longrun-final/resources.jsonl)、[遥测全部资源sidecar](longrun-final/frames.jsonl.resources.jsonl)、[事件](longrun-final/events.jsonl)。

## 1. 实验条件与源码身份

运行根为`/userdata/helmet-validation/20261114-final/longrun-final`，PID3782；系统Linux5.10.160/aarch64、Python3.10.5、OpenCV4.5.4、NumPy1.21.2，真RKNNLite/NPU路径。模型为现有`helmet_416x416.rknn`，SHA256 `7e0eefe4bfe86627bb6b58161cacca10912e0dbe1ba2513f58d091c40b26cd48`，8,110,065B。类别固定`('person','hat')`，person是裸头。

输入是[原回放](replay/manifest.json)的单照片生成640×480录像：1200帧、10FPS、120秒，循环读取；录像SHA256 `f22897e1ebb40cc919caab335ee4b436d28ebaf0a138eb8b16c63166184af5cc`。配置conf .35、NMS .45、skip0、TTL1秒、请求replay10FPS、save interval30秒，无物理摄像头/显示/Web/音频/MPP并发。不能与精度操作点conf .25或量化benchmark处理E2E混为一组条件。

| 执行文件 | SHA256 |
| --- | --- |
| pipeline_runtime.py | `dee30d6b71c9e8d3e697657fb309fb22e8fb47fafd366d60b555466cf56152e9` |
| telemetry.py | `da4522206053712484a21f80bb26d6424649a263f606f35fa16070be51a83b9e` |
| yolo_post.py | `fae57bbfa1d47d5ab16ef444096fab4fe8e14701c5ec10d87b1ec518282b0505` |
| sources.py | `ea89d66fdefb2bc429ca74efa0437a3c0a8aab672a856078fc15afccfe3b31c9` |
| alarm.py | `d3e6d68c7b17f5508f0d697e96998f556f224f9f5067169d66c35972b3a023d2` |
| verify_runtime.py | `02a22caf13a6c514f372f41e3d712fbbda03edf99590a6b1e0bb556e23ae9638` |

本地逐个重算上述6文件SHA与manifest相同。manifest直接枚举这6文件，未枚举每个导入模块；不能说该表独立证明所有执行文件。最终运行包及额外源码冻结记录见[最终validation清单](final-validation-manifest.json)和[final包核验](offline-bundle-network-evidence-verification-final.json)，与长跑原始枚举的覆盖范围分别报告。

## 2. 时长、吞吐与库存

manifest为completed/exit0，实际engine calls17,591。记录UTC11:39:30.995543至12:09:35.071502；控制器总时1804.076033秒。事件中的active start36533.867407509、stop38333.93585132，**有效区间1800.068444秒**；session1800.064616秒。控制器初始化/收尾不充当运行覆盖。

| 项 | 实际值 |
| --- | ---: |
| captured | 17,630 |
| consumed / detected / actual engine calls | 各17,591 |
| capture overwritten | 39 |
| output | 17,590 |
| pending output | 1 |
| active skip / read failure / detection failure / pending capture | 均0 |
| capture FPS | 9.794092859 |
| detection FPS | **9.772426970** |
| output FPS | **9.771871434** |
| display FPS | 0 |

captured=consumed+overwritten，consumed=detected，detected=output+pending_output，库存守恒；35,181条frame记录包含17,591 detection与17,590 output。最后一检测未输出的pending保持在原session，不能改成“每次检测均已完成输出”。`throughput_target_met=false`，不四舍五入为10FPS。

检测capture、processed、output dispatch、sink completion的最大间隔为.262429/.291829/.290766/.290663秒；独立resources1760条、最大间隔1.162232秒，最后样本在active stop前.223145秒。strictcheck检查startup/stop、原帧身份、时间、模型/配置、数值有效性和资源field覆盖，passed=true、errors=[]。本报告调用实际validator对本地原始副本重新核算得到同一结论，不仅引用manifest自带pass；四核心artifact哈希与字节均一致。

## 3. 精确时延与资源保留

| 阶段 | count | mean ms | p50 ms | p95 ms | p99 ms |
| --- | ---: | ---: | ---: | ---: | ---: |
| preprocessing | 17,591 | 12.973 | 13.193 | 17.522 | 20.796 |
| inference API | 17,591 | 48.392 | 45.055 | 72.174 | 79.770 |
| postprocessing，含三头验证 | 17,591 | 29.559 | 28.769 | 43.317 | 54.310 |
| capture→callback dispatch | 17,590 | 115.263 | 112.345 | 166.360 | 203.234 |
| capture→sink completion | 17,590 | 115.602 | 112.703 | 166.693 | 203.552 |

本轮显式启用帧与session日志，六时延均标`exact_linear_disk_spool`；最终精确分位通过磁盘spool计算，live retained limit每字段2048。session资源total_count1781、retained_count1024、全部1781条在`frames.jsonl.resources.jsonl`，不是session数组包含全部资源。validator独立采样的`resources.jsonl`为1760条，两套采样器/时间轴不同，不能混加或把数量差当丢失。

这次验证的是当前**有界live统计与显式完整磁盘证据模式**，不是默认无日志模式的独立板端对照。显式raw logs和排序/磁盘page-cache成本随长度增长；最后active stop后的排序/关闭过程不在上述资源覆盖内。callback completion不是浏览器渲染/屏幕扫描/传感器曝光。原[longrun-1](longrun-1/manifest.json)保留旧精确全列表源码及旧RSS曲线，未被替换或追认为当前执行。

## 4. RSS趋势、温度和频率

![当前源码长跑资源与吞吐](stability-figures/longrun-final-resources.png)

图件另有[PDF](stability-figures/longrun-final-resources.pdf)。原样本首RSS75,624,448B、末136,167,424B、峰137,248,768B，首末增60,542,976B包括NPU/SDK初始化。active开始后第一独立样本131,231,744B，到末增4,935,680B；不能把冷启动到末端差解释成纯泄漏量。

预先在报告中明确描述性分析窗口为**active start+60秒之后**，保留满足该条件的1700原始样本，无删除离群点：第一monotonic36593.944008893、RSS133,287,936B，最后38333.712706172、RSS136,167,424B，增**2,879,488B（2.746094MiB）**。RSS范围132,792,320–137,248,768B；用全部该窗口样本对elapsed seconds做普通最小二乘，斜率**1261.114128B/s，约.072162MiB/min**。60秒是本报告固定描述窗口，不是实验前的验收阈值或已证明warmup完成；没有声明无泄漏、无限运行或内存目标已通过。

两个thermal zone全程范围47.777–62.777°C、45.555–58.333°C，末53.75/51.875°C；温度曲线中实际峰值保留，不猜测成因。独立采样的NPU频率均900MHz，不能由离散读数断言没有任何瞬时变化/热限频。进程CPU均107.629%、范围54.560–122.698%，可超过单核100%。此组测量不包含摄像头/Web/MPP的组合开销。

## 5. Governor、SSH与清理边界

manifest governor记录before=`rknpu_ondemand`、requested=null、after=`rknpu_ondemand`、restore_success=true，未为本轮强制修改策略。运行期间freshSSH[观察](longrun-final/ssh-observation.json)记录同PID3782、startID3652955、匹配命令和launchSSH关闭后的存活，当前validator重新核验身份true；这是外部操作观察，不是物理LAN断开。manifest自带`ssh_survival=not_observed`未因此改写，额外观察单独引用。

**本长跑direct SDK cleanup没有直接记录。** verifier报告`direct_recorded=false`、`accepted=false`，basis为“legacy normal stop is indirect evidence only”。正常停止/exit0只支持间接清理；实际[faultsuite-2](faultsuite-2/manifest.json)的直接SDK收据不能转给这个长跑。`completion-observation.json`的6条仅记录当时running/pid存在，不是终态成功证据；最终结论取原manifest/session/strictcheck。

snapshots不在原4核心artifact哈希inventory内；周期图片可作为功能产物，但不宣称全部快照成对/哈希完整性由该校验器验证。没有强杀线程/断电/native挂死的长跑清理保证。

## 6. 可用于论文/答辩的结论

当前代码在固定10FPS录像回放、conf .35/NMS .45/skip0、无显示的RK3568真实NPU运行中完成1800秒连续覆盖，17,591次检测，显式disk-spool分位和全部资源sidecar可复核。检测/输出仍未达10FPS；active+60秒后RSS增长约2.75MiB、存在正趋势，不能称零增长/无泄漏。清理仅间接记录。

这完成当前源码30分钟回放测量及连续性统计任务，保留性能、内存及证据短板。物理camera格式/FPS/拔插、人工屏显/声音/手机、场景多样性与标注事件质量另验。正式独立85%指标、最终模型和“全场景稳定可靠”不由此证明；不加入训练、微调、剪枝或蒸馏来补写完成度。
