# 基于 RK3568 的离线安全帽检测与状态闭环系统

基于现成社区 YOLOv5 模型、RKNNLite、OpenCV 和 NumPy 的板端检测项目。当前范围明确为**不训练、不微调、不剪枝、不蒸馏**，重点完成可信流水线、可追溯评估、故障处理和部署验证。原开题中的训练路线保留为历史材料，不作为当前待完成项。

## GitHub 保存范围

本仓库保存可复核的源码、测试、模型、当前 v4-r2 应用发布包、Markdown/HTML/PPTX 文档、实验清单与关键验收收据。以下内容保留在本机工作区，不进入普通 Git 历史：`.runtime/`、`.venv/`、`.tmp/`、`.superpowers/`、`datasets/raw/`、外部数据图片、超大视频/JSONL 日志、重复历史 ZIP 和含个人信息的学校模板。

当前运行包 `dist/helmet-native-app-20261115-v4-r2.zip` 已保留；旧 v1/v2/v3 ZIP 只在本地归档，不重新打包或改写。数据来源、许可、选择规则和文件清单仍保存在 `datasets/provenance/` 与 `datasets/evaluation/*/manifest.json`，原始素材按原许可与体积边界另行保存。

摄像头现已接通：[USB相机接入记录](docs/experiments/2026-11-15/摄像头到位与接入准备记录.md)确认`/dev/video10`为真实图像节点、`/dev/video11`为不可采集的metadata节点；[核心短测](docs/experiments/2026-11-15/camera-npu-short-1-verification.json)约12.07秒完成174次检测/173次输出，检测吞吐14.41FPS。v4-r2已在真实USB相机、720×1280全屏、运行中拔线TTL、回桌面常驻、双图标复用、UI退出和196.67秒连续检测上完成本轮验收。现场目标正确性、长期供电和长期现场运行继续独立验证。


截至2026-11-15，板屏采用Qt Quick本地App，日常检测无需板端浏览器或持续USB连接电脑。[当前v4-r2包](dist/helmet-native-app-20261115-v4-r2.zip)/[发布清单](docs/experiments/2026-11-15/native-app/bundle-manifest-v4-r2.json)为39,227,461B、35 entries，SHA `14b56543f7aca0b26c61b663770f8bf54729d0ba61a1ac5069f4f1143e084251`。正式根为`/userdata/helmet-validation/20261115-native-app-v4-r2-local-2`，四个本项目拥有的入口链接和两个desktop均已迁移并备份，`apk3.cfg` SHA保持不变。Phase 1/Phase 2六项核心验收、图标迁移与399项全量回归通过；`systemui`尚未重启，主界面重载后的实体图标显示和手指点击仍需人工确认。先看[App手册](docs/app/板端App使用手册.md)、[v4-r2更新手册](docs/app/离线更新与图标安装-v4.md)与[v4-r2终验](docs/experiments/2026-11-15/native-app-usability-v4-phase2-1/report.md)。

[历史v3包](dist/helmet-native-app-20261115-v3.zip)/[v3清单](docs/experiments/2026-11-15/native-app/bundle-manifest-v3.json)保持39,196,872B、SHA `a6ddec8f0d5579a62df261fc84a8f92cbce7b27c54ead79e9579f6e87e11058d`；[历史v2包](dist/helmet-native-app-20261115-v2.zip)/[v2清单](docs/experiments/2026-11-15/native-app/bundle-manifest-v2.json)保持39,195,268B、SHA `f8957945cb90084f8c32cff789b07e0e4ca1c08690c493c5ed3593eacb4a4989`；[历史v1包](dist/helmet-native-app-20261115.zip)/[v1清单](docs/experiments/2026-11-15/native-app/bundle-manifest.json)保持39,192,392B、SHA `22585661da80c449fbf85ba65978ba851c73935819298ee2a5de48907824e679`。旧包及各运行身份保留，v4-r2不重记v3的WiFi短验或v2的30分钟回放性能。

最新[native-longrun-1中文报告](docs/experiments/2026-11-15/原生App30分钟验证报告.md)与[严格终验](docs/experiments/2026-11-15/native-longrun-1-verification.json)已completed/exit0：实际1800.051066秒、19,806检测/19,805输出、**11.0030283/11.0024728FPS**，360次active HTTP可解码、自动Qt与直接SDK/source/worker/service/GUI清理通过。本run30分钟与固定10FPS=true、请求20供给=false；单照片录像、既有416、controller/service+私有QML验证wrapper、完整日志/30秒截图，不是正常launch.py、物理相机或全配置接受。

running GUI采样3,443/3,601=95.6123%ready/visible，实际displayFPS/连续可见未验；capture→sink p95/p99=244.6833/323.6640ms不含Qt/物理。+60秒后Python RSS+3.406MiB/OLS+.13012MiB/min，Qt末点−1.551MiB但OLS+.45562，峰183.043MiB，末尾抓图下降不掩盖正趋势、不能宣称无泄漏。ondemand未锁频，thermal最高71.666/65.625°C、900MHz只为离散样本。独立85%、S1/S6、实际触摸/供电/WiFi/相机/声音/手机仍待验。

[smoke4](docs/experiments/2026-11-15/native-smoke-4/run-verification.json)12秒134检测/133输出仅归短run；smoke2无有效GUI图与smoke3 healthy间503仍failed保留。[v2服务复审](.superpowers/sdd/2026-11-14-graduation-scope/native-preview-race-review.md)SHA9a2f7454…，23项22pass/1skip、主树250项245pass/5skip、legacy25项24pass/1NPU skip归各日志。旧包/原指标不回填新源码。

11月14日诊断/量化/故障/硬编/CLI禁网/LAN保持原SHA。旧API longrun-final请求10供给、显式日志/轻量回调，17,591检测/17,590输出，**9.772426970/9.771871434FPS未达10**、旧清理未直接记录。新[API12秒烟测](docs/experiments/2026-11-15/receipts-smoke-1-verification.json)151检测/directcleanup也只归自身；本次App20供给/Qt/JPEG/日志负载与热条件不同，不能从11.0030与9.7724差值宣称缓存修复或纯优化收益。

[核心流水线复审](.superpowers/sdd/2026-11-14-graduation-scope/final-project-rereview.md)关闭五项问题，默认时延2,048样本/资源最近1,024条，显式日志磁盘完整记录/精确分位；[当轮稳定性报告](docs/experiments/2026-11-14/stability-results.md)限定Pipeline API日志模式。[v1终验清单](docs/experiments/2026-11-15/native-app/validation-manifest.json)主机215项与板端WiFi34/service20归旧服务，v2以[修复后250项](docs/experiments/2026-11-15/native-app/local-main-after-preview-fix.log)为准。真实NPU短预览与SDK double测试分开，物理源/触摸/独立供电、WiFi认证/DHCP/保存仍待人工。

[11月14日验证清单](docs/experiments/2026-11-14/final-validation-manifest.json)、[当轮板端155/155](docs/experiments/2026-11-14/full-tests-complete-fixtures.log)和[legacy25项23pass/2Node skip](docs/experiments/2026-11-14/legacy-tests-final.log)保持运行身份，不能追认新增App源码。分析依赖在隔离`.deps`，[总恢复](docs/experiments/2026-11-14/final-governor-restoration.json)ondemand verifiedtrue；旧长跑与新烟测before/after均ondemand，各按自己收据说明。

## 文档导航

当前使用[v4-r2原生包](dist/helmet-native-app-20261115-v4-r2.zip)与[实际清单](docs/experiments/2026-11-15/native-app/bundle-manifest-v4-r2.json)，复制前核对清单中的SHA和文件内容。日常板端入口、正式图标迁移、完整退出和已知限制见[v4-r2终验](docs/experiments/2026-11-15/native-app-usability-v4-phase2-1/report.md)。

- [第一次阅读导航](docs/阅读导航.md)：按阶段总结、手册、开题正文和逐页讲稿了解项目，再进入代码与原始证据。
- [项目蓝图与完整手册](docs/项目蓝图与完整手册.md)：当前结构、CLI、隔离部署、评估规则及验收边界。
- [板端App使用手册](docs/app/板端App使用手册.md)、[离线更新与图标安装 v4-r2](docs/app/离线更新与图标安装-v4.md)：本地Qt界面、手动WiFi、完整包SCP复制与原生图标；不等于空白板系统安装、无线OTA或开机自启动。
- [离线HTML完整手册](docs/deliverables/项目手册.html)、[开题逐页内容与演讲稿](docs/proposal/开题PPT逐页内容与演讲稿.md)和[离线开题演示](docs/deliverables/开题演示.html)：本机浏览器直接打开；初学阅读路线见手册第19节。
- [11月15日阶段总结](docs/experiments/2026-11-15/阶段总结.md)：App、新短烟测与当前待验门槛；[11月14日总结](docs/experiments/2026-11-14/项目阶段总结.md)保留历史运行。
- [当前毕业设计范围](docs/superpowers/plans/2026-11-14-graduation-scope.md)：已批准的不训练路线；其中初始基线和任务勾选由实验负责人单独更新。
- [六模型×两来源运行清单](docs/experiments/2026-11-14/accuracy-matrix-1/run-manifest.json)和[配对比较](docs/experiments/2026-11-14/accuracy-matrix-1/cross-source-comparison.json)：12 次完整 dump/eval、配置、哈希和 governor 恢复记录。
- [数据来源与许可](docs/experiments/2026-11-14/data-acquisition/SOURCE_AND_LICENSE.md)：真实数据、标签与重叠限制。
- [Pipeline API长跑严格校验](docs/experiments/2026-11-14/longrun-final/verification.json)、[故障套件](docs/experiments/2026-11-14/faultsuite-2/manifest.json)和[当轮验收审计](.superpowers/sdd/2026-11-14-graduation-scope/task-final-acceptance-audit.md)：保留原连续性/性能短板与实物门槛。
- [同源量化构建](docs/experiments/2026-11-14/quantization/build/paired-build-manifest.json)、[板端配对终态](docs/experiments/2026-11-14/quantization-pair-1/run-manifest.json)和[离线包说明](docs/experiments/2026-11-14/offline-bundle.md)：配置/哈希、实测与预装OS依赖边界。
- [量化结果与独立审计](docs/experiments/2026-11-14/quantization-results.md)：逐类变化、重算图表及短时均值/热条件/输入哈希限制。
- [当前开题正文建议稿](docs/proposal/开题报告正文-建议稿.md)和[陈述框架](docs/proposal/2026-11-14-开题方向优化与陈述框架.md)：与当前工程范围一致；学校正式题目与门槛仍由导师确认。
- [2026-11-13 历史性能报告](docs/performance/2026-11-13-board-validation.md)、[早期阶段文档](毕业设计-初步文档.md)和[原开题文本](开题报告.txt)：历史条件下的记录，不覆盖当前状态。

## 当前证据应如何理解

416×416 候选的项目 all-point AP50 为 SHWD **0.716366**、HardHat **0.792306**；原 `helmet-640-640.rknn` 为 **0.801844 / 0.823431**。新 `helmet_640x640.rknn` 为 **0.801257 / 0.817045**。这三者不要混同。完整六模型表见手册。

这些是固定来源子集上的诊断指标，AP 保留分数下限 `.001`，操作置信度 `.25`，NMS `.45`；不是官方 COCO AP50:95，也不是官方 SHWD difficult-ignore 基准。两来源训练重叠未知，且存在一张已确认的共同照片，比较记录的独立性为 `unknown`，`accuracy_target_pass=null`。不能宣称独立测试“85% 已通过”，也不能仅凭速度指定最终模型。

[10 秒真板文件回放会话](docs/experiments/2026-11-14/smoke-session.json)完成 125 次独立检测，整段检测吞吐约 **12.39 FPS**，采集约 **15.66 FPS**，输出约 **9.72 FPS**，无显示。请求的 `--replay-fps 20` 是回放配置，不是实测摄像头帧率；该会话不替代物理采集、屏显或长跑验收。

[11月14日Pipeline API30分钟](docs/experiments/2026-11-14/longrun-final/manifest.json)active1800.068444秒、17,591真检测/17,590output/1pending、39覆盖；`.35/.45`/skip0/TTL1、replay10、显式日志，无App/Qt/Web/MPP并发，连续性/资源/库存strictpass。60秒后RSS增2,879,488B（2.746MiB）/OLS .072162MiB/min，不保证无泄漏。原[longrun-1](docs/experiments/2026-11-14/longrun-1/manifest.json)保留旧9.754817FPS与SHA，两run均未通过10。新12秒151检测直接收据只归其run，旧验证器目标绑定replay20而throughput_target_met=false；App长稳/固定10目标另验。

同源新FP16/INT8板端benchmark（各100循环）infer为101.17/41.87ms，E2E为118.49/64.43ms、8.4/15.5FPS；此E2E只含预处理/推理/后处理。INT8相对FP16诊断mAP50变化为SHWD **−0.8493个百分点**、HardHat **−1.1164个百分点**。这两个新配对模型与六模型矩阵中的既有416哈希不同，不能混用指标。

## 项目结构

```text
helmet/                 实时 CLI、流水线、输入源、报警、遥测、输出与板端工具
helmet/app/             本机令牌服务、检测控制、WiFi、启动/图标安装及QML键盘界面
helmet/pc/              转换、VOC/COCO、精度、误检漏检与比较工具
helmet/models/          五个既有转换模型
models/                 原模型、样图；source/ 中有已核对来源 ONNX
tests/                  流水线、源、评估、导出与契约回归
helmet/tests/           既有比较、CLI、Web/切片回归
datasets/raw/           原始数据与标注，保持不变
datasets/provenance/    来源、许可、选择、坐标、照片重叠与哈希证据
datasets/evaluation/    两套固定 200 图的规范化标注、图片与 manifest
docs/experiments/       原始测量、运行清单、阶段总结
.runtime/、.venv/       工作区隔离运行环境；不属于模型成果
.codebuddy/tools/       SSH 不可用时的串口辅助
```

## 板屏入口与隔离CLI回放

日常按App手册选择/开始/停止，v4-r2正式根为`/userdata/helmet-validation/20261115-native-app-v4-r2-local-2`。[v4-r2终验](docs/experiments/2026-11-15/native-app-usability-v4-phase2-1/report.md)确认4个owned链接/2个desktop已迁移并备份、`apk3.cfg` SHA不变，但未重启`systemui`，因此尚未完成主界面重载后的手指点击。正式链接已直接启动新root、复用同一session并完成正常UI退出。历史v2 normal wrapper→launch.py默认原640/1.jpg/replay10短回放、v3 WiFi列表/键盘和lo UP短禁网分别保留原身份，不替代v4-r2当前验收。

首选原生 SSH，串口仅作为兜底。部署入口为 [helmet/deploy.ps1](helmet/deploy.ps1)，`deploy.cmd` 是它的包装器。默认 SSH 别名 `rk`，默认目标 `/userdata/helmet-validation/deployment`；部署复制主脚本、PC 工具、两套测试、六模型和样图，并检查编译、CLI 与模型哈希。新实验使用新的隔离目录，保留既有结果。

应用仍从平铺`src/`运行；完整回归使用独立`repo/`镜像，保留`repo/helmet/pc`、`repo/helmet/tests`、`repo/tests`和验证脚本/requirements。部署自身的compile/help及来源检查通过不等于完整回归树通过：

```powershell
ssh rk "cd /userdata/helmet-validation/demo-20261114/repo && python3 -m unittest discover -s tests -v"
ssh rk "cd /userdata/helmet-validation/demo-20261114/repo && python3 -m unittest discover -s helmet/tests -v"
```

```powershell
.\helmet\deploy.cmd
# 或显式指定隔离目录
.\helmet\deploy.ps1 -Board rk -RemoteRoot /userdata/helmet-validation/demo-20261114
ssh rk "cd /userdata/helmet-validation/demo-20261114/src && python3 helmet_realtime.py --model ../models/helmet_416x416.rknn --source ../input --conf 0.25 --nms 0.45 --replay-fps 20 --result-ttl 0.5 --no-display --duration 10 --jsonl ../results/demo.frames.jsonl --session-json ../results/demo.session.json"
```

Linux 主机使用 `sh helmet/deploy.sh rk /userdata/helmet-validation/demo-20261114`。上述 416 仅为复现性能候选，不表示最终选型。JSONL 路径应每轮新建，避免复用旧日志。

不指定`--jsonl/--session-json`时，完整计数/均值精确、默认分位按reservoir样本并标注方法；指定其中任一参数后保存完整帧/资源sidecar，最终分位标为`exact_linear_disk_spool`。例如`--jsonl demo.frames.jsonl`还生成`demo.frames.jsonl.resources.jsonl`；仅指定session时自动生成其`.frames.jsonl`及资源sidecar。磁盘空间和最终排序成本随运行长度增长。

常用 CLI 和工具：

```powershell
.\.venv\Scripts\python.exe helmet/helmet_realtime.py --help
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe -m unittest discover -s helmet/tests -v
```

板端额外功能参数：`--save-dir/--save-interval`、`--alarm-dwell/--alarm-cooldown/--alarm-clear`、`--web-host/--web-port`、`--clip-dir/--clip-pre/--clip-post/--clip-fps/--clip-encoder`。Web 默认为关闭，启用后默认只绑定 `127.0.0.1`；通过 SSH 端口转发或显式局域网绑定查看。录像默认为 OpenCV 路径；MPP 的短探针与完整事件切片、CPU 对照分别验收。

## 数据和评估

WiFi实际页面wifi-2/3显示“操作失败”且列表为空，后端真实wpa与WifiActions诊断返回16网络；协议追踪已确认v2默认8秒等待过短，实际扫描事件13.8725秒到达；WiFi v3实际UI扫描列表/键盘30秒exit0已补，未真实连接，不能把服务/UI exit0当扫描或连接通过。offline-1缺unshare未起App的工具依赖失败保留。可编辑PPTX草稿以[交付导航](docs/deliverables/README.md)最新验证版为准，学校模板/个人信息需填，不提前链接未完成新版本。

[dataset_tools.py](helmet/pc/dataset_tools.py)负责显式来源类别映射、坐标模式、difficult 规则、原文件名解析和 manifest。[dump_dets.py](helmet/dump_dets.py)加载现有 RKNN、调用共享后处理，逐图记录状态和输入/模型/后处理哈希；失败运行不能评分。[eval_map.py](helmet/pc/eval_map.py)计算两类 AP50/P/R/F1 和面积分组，[analyze_errors.py](helmet/pc/analyze_errors.py)生成 TP/FP/FN 图及严格配对比较。

模型顺序固定 `('person','hat')`：0 是裸头，1 是戴帽头部。HardHat 的全身 `person` 与模型裸头类别不同，35 个全身框明确忽略。SHWD 的 139 difficult 帽目标按显式 include-as-normal 规则保留为普通 GT；7 处文件名声明修正均有原名和哈希留档。两来源均无可靠 scene ID，使用一个未知场景组的诊断子集，不能伪造独立 train/val/test。

## 设备接入与历史环境

已归档板端环境为 Linux 5.10.160/aarch64、Python 3.10.5、OpenCV 4.5.4、NumPy 1.21.2、RKNNLite 2.5.0、librknnrt 2.3.2。同源416 FP16/新INT8均由Toolkit2 2.3.2隔离Linux构建成功并完成真板配对，失败的CPU/AVX尝试与最终governor恢复记录保留。100张原SHWD train图仅作INT8校准，未训练或微调。

[原生App禁网](docs/experiments/2026-11-15/native-app/native-offline.json)仅lo UP用于本机服务，12秒Qt/NPU demo exit0，未切换实际网络。[原/v2 CLI禁网](docs/experiments/2026-11-14/offline-network-isolated/manifest.json)77检测归旧SHA；[旧final CLI包](dist/helmet-offline-20261114-final.zip)SHA `480e7b1cc2c2f37697890e80d44defbc084275610506817043e3bed46853fb2b`及[其禁网终态](docs/experiments/2026-11-14/offline-network-isolated-final/manifest.json)32files/6models、三exit0、79检测/78输出/1pending、仅lo DOWN继续保留。它不包含App，所有版本均需已有兼容OS/runtime，短禁网不替代30分钟或空白板安装。

[活跃LAN状态探测](docs/experiments/2026-11-14/web-lan-live-server-probe.json)取得三次status HTTP200；[最终LAN预览探测](docs/experiments/2026-11-14/web-lan-preview-final.json)在PID3057会话取得五张HTTP200可解码640×480 JPEG，以及MJPEG200与multipart JPEG首帧，服务身份exit0。保存的[实际预览图](docs/experiments/2026-11-14/web-board-preview.jpg)包含检测框/未戴帽横幅/帧号；手机/浏览器人眼演示仍延期，旧超时探测保留。

[实际HeadlessChrome157](docs/experiments/2026-11-14/browser-current-3/browser-observation.json)已记录MJPEG load、naturalWidth640、visible/fresh；[CDP恢复](docs/experiments/2026-11-14/browser-current-3/recovery-observation.json)offlineHidden/recoveredVisible/freshLoadAfterRecovery=true，仅隔离宿主浏览器离线，板端源与真实网络不变。该自动观察不代表手机、人眼或物理WiFi断网。

```powershell
ssh rk "uname -a"
ssh rk "v4l2-ctl --list-devices"
& ".\.codebuddy\tools\rk.cmd" "uname -a"
```

[Qt Wayland显示probe](docs/experiments/2026-11-14/display-probe.json)已完成窗口创建、事件处理和关闭并退出0，保留runtime symlink警告；没有人工板屏视觉确认。不要把该probe或 `/dev/videoN`、`/dev/fb0`、weston存在解释为物理摄像头或最终屏显已通过。先核对实际设备与格式，再做对应验收。串口默认 COM6/1500000 baud，可通过 `RK_PORT`、`RK_BAUD`、`RK_TIMEOUT` 覆盖；账号密钥和 WiFi 凭据保留在主机/板端配置中。

WiFi部分沿用v3已验证的默认整体scan20秒、单命令2秒和显式override；v4-r2包内`wifi.py` SHA为`648b714137b18538f8f8e6a355565b07aec8fb01270c3deed5c01eba30249468`。真实Qt页面已显示SSID列表和密码键盘，原网络保持`COMPLETED`，但没有执行目标网络认证、DHCP、保存和失败恢复，因此当前只证明扫描与输入界面，不证明真实连接。WiFi事务、检测后台和退出共用resident生命周期；联网不是本地检测启动前提。
