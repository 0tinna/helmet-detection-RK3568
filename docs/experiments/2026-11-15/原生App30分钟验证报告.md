# 原生App30分钟验证报告 · 2026-11-15

`native-longrun-1`在真实RK3568上完成App组合回放，原始manifest为completed/exit0，冻结验证器的完整性、自动Qt渲染、30分钟及固定10FPS门槛通过。实际active1800.051066秒，19,806次独立检测、19,805次输出，检测11.0030283FPS、输出11.0024728FPS；360次active HTTP预览均可解码，直接SDK/source/worker/HTTP/App/GUI退出收据接受。

该结论只适用于本次既有416模型、单照片录像循环、响应回放供给和显式证据负载。没有验证物理摄像头、人工看屏/触摸、真实WiFi切换、连续可见或实际显示FPS，也没有内存无泄漏验收结论。独立85%、S1来源/场景与S6最终配置仍未通过。

## 1. 证据来源与身份

原始证据为[native-longrun-1 manifest](native-longrun-1/manifest.json)、[session](native-longrun-1/session.json)、[cleanup](native-longrun-1/cleanup.json)、逐帧/资源/HTTP/GUI日志和截图。协调者确认Python PID8082与Qt PID8095退出后，重新复制全519项原始文件，并用冻结验证器生成[严格验证结果](native-longrun-1-verification.json)，退出0、passed=true/errors空。

[资源/时延派生结果](native-longrun-1-stability-v1/stability-results.json)及[英文分析稿](native-longrun-1-stability-v1/stability-report.md)分别保留公式、全精度值、过程分窗和图件；[协调者独立QA](../../../.superpowers/sdd/2026-11-14-graduation-scope/native-stability-root-longrun1-qa.json)确认519项raw未改、24个时延分布和进程/GUI窗口计数与独立stdlib统计一致，strict/thirty-minute均true。本中文稿读取这些实际终态和清理记录，并检查三张派生PNG；没有重新运行板端或改写raw/原manifest。

| 项目 | 本run固定身份与条件 |
| --- | --- |
| 模型 | 既有helmet_416x416.rknn，8,110,065B，SHA `7e0eefe4bfe86627bb6b58161cacca10912e0dbe1ba2513f58d091c40b26cd48` |
| 输入 | construction-replay.avi，82,400,444B，SHA `f22897e1ebb40cc919caab335ee4b436d28ebaf0a138eb8b16c63166184af5cc`；单照片生成录像循环 |
| 输入尺寸/源 | 模型416×416；实际解码640×480 VideoSource；文件头10FPS/1200帧，请求回放20FPS |
| 服务 | v2 service.py 29,859B，SHA `9a2f7454db6fb48ebcf7f5fd301a831817374c2be2fc5c9036af60feb40df1f7`，部署根`/userdata/helmet-validation/20261115-native-app-v2` |
| 运行与验证器 | verify_native_runtime.py 66,884B，SHA `222d07022ba746d5fd9bcdb2c5a8924c2b383473585311cf0dce5b0d9fc93997`；共享verify_runtime SHA `fdc94bf540d83e8f9a8367d9004d171e60127bb798ba8a6c113999ed888b6217` |
| 工作点 | conf.35/NMS.45/skip0/TTL1秒，profile responsive；类别person=裸头、hat=戴帽头部 |
| 实际路径 | 真实DetectionController/LocalService/Pipeline/RKNNLite、当前QML与私有验证wrapper；未执行正常launch.py或厂商图标入口 |
| 证据负载 | 完整逐帧JSONL/session/资源sidecar、每30秒标注截图、每5秒额外HTTP探针；QML实际5Hz预览/2Hz状态，wrapper另有.5秒控制与只读可见性采样、末5秒ready抓图 |
| 环境 | 预装Linux5.10.160/aarch64、Python3.10.5、OpenCV4.5.4、NumPy1.21.2、Lite2包2.5.0；实际runtime2.3.2、driver0.9.2，Qt Wayland/qmlscene |
| 调频/网络 | governor前后rknpu_ondemand、requested=null，未锁频/未改策略；WiFi仅status，未切换/认证/DHCP |

模型、Pipeline/后处理/工作点没有训练或调整。v2服务修复预览旧reader误清新缓存，仍以稳定JPEG/frame/采集时间/epoch/generation/TTL封包并拒绝失效。它与旧服务、GUI负载、节拍及热条件的变化是整体配置变化，不能从两run FPS差宣称单一修复的纯加速。

## 2. 时长、库存与分阶段吞吐

active单调区间为71506.736018384至73306.787084853，实测1800.051066469秒；Pipeline session为1800.049895689秒。发布吞吐采用冻结验证器observed_rates：原始独立计数除session时长，检测11.003028331FPS/输出11.002472791FPS。另按actual active重算分别11.003021175/11.002465635FPS，两种时长分列且固定10门槛均不变，没有把启动或收尾sleep补作检测30分钟。

| 项目 | 原始计数或实测 |
| --- | ---: |
| captured / consumed=detected | 34,496 / 19,806 |
| capture overwritten / pending capture | 14,689 / 1 |
| output / pending output | 19,805 / 1 |
| skip / read failure / inference failure | 0 / 0 / 0 |
| 实际engine调用 / 实际source读取 | 19,806 / 34,497（一次读成功未进入captured库存） |
| 采集 / 检测 / 输出FPS | 19.1639132 / 11.0030283 / 11.0024728 |
| OpenCV displayed计数 | 0；本run采用Qt，实际Qt显示FPS未测 |
| active HTTP / 可解码预览 | 360 / 360 |
| frame日志行 | 39,611=19,806 detection+19,805 output |
| 独立resource / Pipeline resource / event / HTTP行 | 1,732 / 1,746 / 64 / 1,087 |

库存守恒为34,496=19,806+14,689+1，19,806=19,805+1；停止时待输出不隐藏或写成全部已完成。请求20是供给配置，实际captured19.1639；固定10FPS检测/输出门槛true，请求20供给目标false。源没有场景多样性，连续回放不能评价真实摄像头协商、业务事件质量或独立测试精度。

## 3. 回调分发与sink完成延迟

下表使用原始全量日志线性分位`(n−1)×q`，未过滤慢帧；完整JSON保留精度。前三项各19,806次，后三项各19,805次。

| 阶段 | mean ms | p50 ms | p95 ms | p99 ms |
| --- | ---: | ---: | ---: | ---: |
| 预处理 | 15.1128 | 14.3413 | 23.7737 | 28.7147 |
| NPU推理 | 47.7617 | 46.8319 | 64.4070 | 72.4229 |
| 后处理 | 24.7696 | 19.4694 | 50.7900 | 67.5192 |
| capture→callback dispatch | 137.0245 | 133.4628 | 191.8008 | 254.9985 |
| callback sink | 44.2283 | 40.5967 | 75.0997 | 90.1982 |
| capture→sink completion | 181.4646 | 176.6041 | **244.6833** | **323.6640** |

sink completion截至本机回调返回，不含之后HTTP传输、Qt JPEG解码、屏幕扫描或摄像头曝光。不能将244.6833ms称为人眼端到端延迟，也不能把显示刷新率当检测吞吐。末300秒分位与完整active分别保留：[尾延迟图](native-longrun-1-stability-v1/figures/latency-active-tail-p95.png)只比较这些窗口，不能建立固定热条件的因果收益。

![全active与末300秒阶段p95](native-longrun-1-stability-v1/figures/latency-active-tail-p95.png)

## 4. Python与Qt资源分别报告

窗口为实际active、active+60秒后和末300秒。60秒是描述分析的排除窗口，不证明预热完成。RSS分别属于Python PID8082与Qt PID8095，共享映射可能重复计入两进程，不把两RSS相加称唯一物理内存。

| 窗口/进程 | 样本 | 首MiB | 末MiB | 峰MiB | 首末变化MiB | OLS MiB/min |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| active Python | 1,728 | 124.215 | 137.730 | 140.020 | +13.5156 | +0.13555 |
| active Qt | 1,728 | 159.316 | 166.055 | 183.043 | +6.7383 | +0.44887 |
| +60秒后 Python | 1,670 | 134.324 | 137.730 | 140.020 | **+3.40625** | **+0.130122** |
| +60秒后 Qt | 1,670 | 167.605 | 166.055 | **183.043** | **−1.55078** | **+0.455617** |
| 末300秒 Python | 288 | 137.957 | 137.730 | 140.020 | −0.22656 | −0.00261 |
| 末300秒 Qt | 288 | 178.344 | 166.055 | 183.043 | −12.2891 | −0.09792 |

OLS按真实单调采样时间做无权线性回归，+60秒后Python均值CPU约144.218%、Qt约52.533%，多核进程百分比可以超过100%。Qt末尾ready抓图/收尾附近RSS下降使首末差为负，而多数窗口与OLS仍为正；不能选末点下降掩盖+0.455617MiB/min正趋势或宣称内存优化通过。它是单run描述，未设泄漏阈值，既不证明无泄漏，也不单凭正斜率认定泄漏。

两thermal节点最高71.666°C/65.625°C；离散NPU样本均900MHz。governor前后ondemand并未锁频，采样900MHz不能排除瞬时变化或代替CPU频率。保留Qt step、离群值与末点，不平滑删除。

![Python与Qt分别RSS/CPU及实际时间窗口](native-longrun-1-stability-v1/figures/process-rss-cpu.png)

## 5. Qt渲染与采样可见性的边界

当前Main.qml自动渲染和末5秒ready内容截图已验证，[原始screen.png](native-longrun-1/screen.png)与GUI退出收据绑定本run。全部GUI观察3,604项有3,443个ready/visible inline-JPEG样本；限制在control phase running时为**3,443/3,601=95.6123%**。这是半秒间隔观察比例，不是显示帧数、连续可见时间、全程无黑屏或实际显示FPS。不可用样本保留于原始记录，不能以一张有效截图宣布连续显示通过。

截图的状态frameID来自独立轮询，不能自动等同正在显示JPEG的身份；JPEG/frame/generation/epoch/TTL由实际HTTP包和服务验证。Qt图像解码/切换短暂不可见与检测系统可用性是不同指标，连续显示门槛没有据此通过。

![GUI ready/visible与不可用采样，非显示FPS](native-longrun-1-stability-v1/figures/gui-sampled-availability.png)

## 6. 清理、历史与仍待验收的事项

长跑后[安装/独立运行报告](原生App安装与独立运行验证.md)新增v2入口迁移、actual wrapper→normal launch.py及child仅lo UP短NPU/Qt exit0/private clean/父网络保持，补正常启动与运行时外网依赖证据。默认原640/1.jpg/replay10截图6.2FPS仍非本416长跑，normal launcher未做30分钟；无人工图标点击/触摸/供电。WiFi actual页扫描失败/空列表与backend/WifiActions16网络成功已定位v2默认8秒等待过短/13.8725秒事件迟到，WiFi v3实际列表/键盘30秒exit0已补，仍未真实连接，没有连接/DHCP/保存通过。

直接cleanup记录engine/source初始化与释放、SDK created/released=true/release_error=null、capture/detect worker不存活、shutdown_timeout=false；service检测/WiFi事务停止、HTTP/App worker不存活。Qt PID8095退出0、日志线程停止、图片保存。本run清理accepted，不能回填旧Pipeline longrun-final缺直接收据，也不保证断电/SIGKILL/native挂死能finally清理。

旧smoke2无有效GUI图和smoke3 healthy间HTTP503保持failed；v2缓存修复后的smoke4短run与本长跑分别留原SHA。旧Pipeline API10FPS供给长跑9.7724FPS仍未达10；新App20供给/日志/Qt/JPEG/截图与温度条件不同，不能把11.0030−9.7724的差全部归因于缓存修复、模型变化或某纯优化。

本run的严格完整性、采样HTTP策略、自动Qt渲染、30分钟组合和固定10FPS门槛通过。以下仍未闭合：连续可见/实际显示FPS、预设内存无泄漏验收、正常launch.py/实际图标入口的同配置长跑、独立供电/真实触摸、WiFi认证/DHCP/保存/失败恢复、物理摄像头/声音/手机、独立85%/S1与S6最终模型配置。既有模型不训练、不微调、不剪枝、不蒸馏；最终选择需联合质量、实际使用与稳定性。

[v2包](../../../dist/helmet-native-app-20261115-v2.zip)/[实际清单](native-app/bundle-manifest-v2.json)已归档39,195,268B/28payload，SHA `f8957945cb90084f8c32cff789b07e0e4ca1c08690c493c5ed3593eacb4a4989`，复制前核对内部身份。两份App包内手册哈希固定，本报告/未来入口与禁网记录在外部维护，不重打包。v1包22585661…、原图标/lo UP禁网保持历史，v2入口迁移与正常launcher/offline短验按新安装报告归档，不转记为本次416长跑。

WiFi-only v3当前修订见[安装与实际UI验证](原生App安装与独立运行验证.md)：wifi SHA648b714137b18538f8f8e6a355565b07aec8fb01270c3deed5c01eba30249468，默认整体scan20秒/command2秒，显式override保持；实际normal wrapper/launch.py30秒SSID列表与键盘exit0/private clean、原网络COMPLETED，未真正连接。安装根仍历史名20261115-native-app-v2，service/Pipeline/QML未改。v2包f8957945…与416组合30分钟原SHA/性能保持，不能称v3新长跑；[v3实际清单](native-app/bundle-manifest-v3.json)记录39,196,872B/28项，ZIP SHA `a6ddec8f0d5579a62df261fc84a8f92cbce7b27c54ead79e9579f6e87e11058d`；[板端安装收据](native-app/v3-installed-release.json)确认28项匹配，旧失败保留。同WiFi v3 normal wrapper/launcher/NPU/Qt离线短run exit0，child仅lo UP/private clean/父网络保持；Linux WiFi35/35协议fixture全过，非真实连接或新的组合30分钟。
