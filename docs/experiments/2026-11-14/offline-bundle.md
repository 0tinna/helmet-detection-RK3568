# RK3568 离线运行包与依赖边界

当前交付为冻结最终源码后新建并校验的 [helmet-offline-20261114-final.zip](../../../dist/helmet-offline-20261114-final.zip)（39,764,098字节，约37.92 MiB）；包含32个载荷文件和1份manifest。历史 [原ZIP](../../../dist/helmet-offline-20261114.zip) 与 [v2 ZIP](../../../dist/helmet-offline-20261114-v2.zip) 完整保留。原/v2当时源码相同，ZIP字节与哈希相同；final包含后续有界遥测、输出校验、模型头校验与基准清理等最终源码，因此是不同字节产物。以下为当前final压缩包SHA256：

```text
480e7b1cc2c2f37697890e80d44defbc084275610506817043e3bed46853fb2b
```

单独的 [final ZIP校验值](../../../dist/helmet-offline-20261114-final.zip.sha256) 应通过可信传输路径与ZIP一同保留。内部哈希能验证载荷与manifest是否一致，不能替代签名；若有人同时重写manifest和ZIP，还须依靠独立保存的ZIP哈希发现变化。final manifest SHA256为 `96c0b592b72773b4392f3bbb664a533a5bfc0b8725e3c040cbd7d21482a29e6e`。

此包用于**已有可工作的Linux aarch64 / Python3.10 RK3568板端环境**，不训练模型，也没有切换生产默认配置。它不是可在空白新板完全无网安装所有依赖和操作系统的镜像。`offline_fresh_board_install_complete` 明确为false。

包内包含10个运行/验证模块、4个PC工具源码、原640模型与5个现有尺寸模型、`models/1.jpg`样例、独立校验器、包内本地README、配置/依赖状态、模型来源与哈希、board/host依赖说明，以及官方Lite2轮子和其来源记录。源ONNX可用 `--include-onnx` 显式加入，当前默认包未加入。原始数据集、`.runtime`、虚拟环境、SSH密钥、凭据、缓存和新量化对照模型不在包内。源模型/图片的再分发权没有由此包授予；这是当前用户授权的本地工程传输产物，不作为公开数据/模型发布许可。

`person`仍表示裸头，`hat`表示戴帽头部。包内配置只记录暂定416响应优先/新640小目标质量优先配置及 `.001` AP下限 / `.25` 操作阈值 / `.45` NMS / `.50`匹配IoU；不改变CLI现有默认阈值。两来源诊断mAP50均未达到85%，训练独立性未知；摄像头、显示和稳定性门槛仍需实际独立证据。

## 依赖实际状态

| 项目 | 当前已记录板端状态 | ZIP是否提供 | 无网新板安装限制 |
| --- | --- | --- | --- |
| Python | Linux aarch64 / 3.10.5 | 否 | 需已有板端OS解释器 |
| NumPy | 1.21.2 | 否 | 当前Buildroot包；未准备其对应二进制/系统依赖离线安装集 |
| OpenCV | `cv2.__version__=4.5.4`，无opencv-python分发metadata | 否 | 当前Buildroot编译模块及codec/GUI库；不能用猜测pip版本替代 |
| RKNN Lite2 | 2.5.0 | 官方cp310 aarch64轮子 | 仅Python轮子；已工作的板无需重新安装 |
| psutil | 5.8.0 | 否 | Lite2声明的依赖，当前板已具备；新板需另备兼容包 |
| ruamel.yaml | 0.17.31 | 否 | Lite2声明的依赖，当前板已具备；新板需另备兼容包 |
| kernel/NPU driver/librknnrt | 已记录driver0.9.2、runtime2.3.2 | 否 | 需匹配RK3568板端厂商栈；Python ZIP不能安装完整厂商系统 |

`requirements-board.txt`把Buildroot的cv2作为注释中的模块合同，未写成虚构的pip opencv版本；独立校验器直接import cv2并比对4.5.4。`requirements-host.txt`描述现有host分析环境，未附Windows/Linux x86所有离线依赖轮子；包含转换工具源码也不意味着完整转换环境已经打包。

官方Lite2文件：

```text
rknn_toolkit_lite2-2.5.0-cp310-cp310-manylinux_2_17_aarch64.manylinux2014_aarch64.whl
bytes: 574389
SHA256: 57a5a1f967ff88a1f933abc15a64cf11db37fb406c00cb610f20a566ec231161
```

固定官方仓库commit为 `2c723177a94fac65cbc036d722907ddbc91980cf`，原Git blob SHA1为 `6ad701096a6bad07fa7946dccdf49bbee72de51a`。下载地址和轮子自身METADATA（Requires-Dist: numpy、psutil、ruamel.yaml）保留在 [wheel-provenance.json](../../../dist/deps/wheel-provenance.json)，下载文件来自 [airockchip官方固定commit](https://raw.githubusercontent.com/airockchip/rknn-toolkit2/2c723177a94fac65cbc036d722907ddbc91980cf/rknn-toolkit-lite2/packages/rknn_toolkit_lite2-2.5.0-cp310-cp310-manylinux_2_17_aarch64.manylinux2014_aarch64.whl)。未用其他平台/版本轮子冒充板端2.5.0。

## 校验与部署步骤

生成器 [build_offline_bundle.py](../../../helmet/pc/build_offline_bundle.py)仅使用Python标准库，运行时不访问网络、不调用NPU。明确允许的源码文件名被逐项读取，缺失任何模块/模型即失败；不递归收集仓库中的任意文件。重建拒绝覆盖现有ZIP，实验结果应使用新路径。

```powershell
.\.venv\Scripts\python.exe helmet/pc/build_offline_bundle.py verify dist/helmet-offline-20261114-final.zip
.\.venv\Scripts\python.exe helmet/pc/build_offline_bundle.py extract dist/helmet-offline-20261114-final.zip NEW_LOCAL_DIRECTORY
```

`extract`在创建目标目录之前验证全部档案路径、重复成员、成员是否在manifest、六模型完整性、每个文件的字节数/SHA256；既有目标目录拒绝复用，避免与旧产物混合。它拒绝绝对路径、`..`、反斜线、Windows盘符/流路径、保留设备名、symlink成员。ZIP只含常规文件，没有自动执行安装或脚本。

将完整解包目录拷贝到板端新隔离目录 `/userdata/helmet-validation/NEW_BUNDLE`，再运行包内独立校验器。所列命令仅作为协调owner板端验收入口，本打包任务没有执行SSH或NPU推理。

```sh
cd /userdata/helmet-validation/NEW_BUNDLE
python3 verify_bundle.py verify .
python3 verify_bundle.py board-check
python3 -m compileall -q src
mkdir results
cd src
python3 helmet_realtime.py --help
python3 helmet_realtime.py --model ../models/helmet_416x416.rknn --source ../input --conf .25 --nms .45 --no-display --duration 10 --session-json ../results/replay-session.json
```

`verify`和`board-check`不进行推理。前者逐项检查载荷哈希；后者打印Python平台、NumPy、cv2、Lite2、psutil、ruamel.yaml的实际import/版本及错误，要求Linux aarch64 Python3.10和已记录核心版本。import通过仅表示能继续NPU验证，不能证明摄像头、显示、codec、长跑或完整无网新板安装。

已具备所有依赖并且ABI匹配时，包内Lite2可通过 `python3 -m pip install --no-index --no-deps deps/*.whl` 单独离线安装。当前已工作的板不需要这样替换包；该命令故意不尝试联网拉取缺失依赖，更不能自动补齐OS厂商栈。需要先逐项确认依赖，不能看到有一个轮子就声称所有新板依赖齐备。

## 已验证和待验证证据

本地已执行：

- 11个边界回归，10通过、1因Windows不允许创建源码symlink跳过；ZIP symlink成员拒绝用例在所有平台实际通过。覆盖模型/运行模块缺失、不安全路径、ZIP重复/额外成员、篡改载荷且不创建解包目录、目录篡改、拒绝覆盖、凭据/私有Python文件排除、错误轮子哈希和字节确定性。
- 真实37.92 MiB ZIP完整验证，32个载荷、6个模型，随后解包并用独立校验器再次验证。
- 原/v2及final真实解包源码的独立校验、`compileall` 与 `helmet_realtime.py --help`全部退出0。当前 [final本地核查](offline-bundle-local-verification-final.json)记录14个运行/PC源码SHA256与冻结仓库源码逐项一致；历史日志保留为 [原版](offline-bundle-local-verification.json) / [v2](offline-bundle-local-verification-v2.json)。
- final的 `src/output_sinks.py` SHA256为 `c2f65642d38f8994200503b242ca93c8f0af105dc5ad3b13650e9ff83c7003cf`；`src/telemetry.py`为 `da4522206053712484a21f80bb26d6424649a263f606f35fa16070be51a83b9e`。历史原/v2的3b7输出源码属于当时版本，不再充当当前final源码身份。
- 打包阶段的历史两棵回归树分别执行132项和23项，退出0；各1个跳过（第一棵为Windows源码symlink权限，第二棵为未设置真实NPU测试模型）。对应 [host回归日志](offline-bundle-regression-1.txt) 和 [legacy回归日志](offline-bundle-regression-2.txt)；最终生产源码完整回归由协调owner另行记录，不把这两份历史日志重新标为final源码测试。final档案核查后再次运行11项打包边界回归，10通过、1同平台权限跳过。

协调owner已对**final包**在板端临时网络命名空间实际完成离线NPU运行；本任务随后读取并核查了原始 [final隔离manifest](offline-network-isolated-final/manifest.json)、[final session](offline-network-isolated-final/session.json)、[final逐帧记录](offline-network-isolated-final/frames.jsonl)、[全量资源sidecar](offline-network-isolated-final/frames.jsonl.resources.jsonl) 和 [final离线证据核查JSON](offline-bundle-network-evidence-verification-final.json)。`unshare(CLONE_NEWNET)` 的子进程命名空间为 `net:[4026532925]`，接口只有 `lo DOWN`，manifest记录 `host_network_changed=false`；主机/SSH网络没有永久更改。

同一final隔离执行记录中，包内32载荷/6模型完整性校验、真实依赖import检查和416×416 RKNN样例回放三个命令均退出0；运行时bundle manifest SHA256为 `96c0b592b72773b4392f3bbb664a533a5bfc0b8725e3c040cbd7d21482a29e6e`，与final ZIP逐字节读取的manifest相同；14个源码与当前冻结源码一致。六模型字节不变。此处依据final自身实际运行，而非沿用旧包推理身份。

final的10.069秒session记录79次实际捕获/消费/检测、78次输出、0读错误、0推理失败、0显示，结束时1个待输出结果；逐帧JSONL含79个detection和78个output，输出ID及捕获时间都与对应检测绑定。资源sidecar实际有9条完整资源记录，逐条与session资源记录相同并保持时间单调；短跑资源记录不代表长期内存稳定性。实际记录1张alarm与4张periodic共5张快照，保存时间均早于该结果TTL截止时间。检测率为7.846 FPS，输出率7.747 FPS；NPU时间p50/p95/p99为40.28/48.08/48.51 ms，回调调度捕获到输出p50/p95/p99为93.30/105.12/113.58 ms，sink完成为93.60/105.42/113.85 ms。这些是final禁网单图回放的session测量，不能替代真实摄像头/显示率、长跑稳定性或run2单图基准。

历史原/v2包的独立禁网运行也保留：对应 [历史manifest](offline-network-isolated/manifest.json) 与 [历史核查JSON](offline-bundle-network-evidence-verification.json)，bundle manifest为 `bfa210a69d6ebe8bcc18e8e9aad1116e24caac4385cdf65a5a8e2373f484dec3`，当次10.048秒77检测/76输出。历史包SHA256 `ee3e446b85511b353cac101b9548c3edc53bdf0dab9c9535e66dd724b18a5c92`与final不同，分别绑定各自源码与运行证据。

源码校验与软件测试是软件证据；一张样例回放即使在禁网子进程跑通，也不代表物理摄像头、显示、全新板离线安装或30分钟稳定性通过。
