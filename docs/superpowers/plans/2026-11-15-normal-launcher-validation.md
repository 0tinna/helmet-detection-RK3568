# 正常启动路径回放验证实施计划

> **最新调整（2026-11-15）：** N1/fix2及N2a均完成独立代码审查，安全校准probe和固定416正常入口[约50秒短验证](../../experiments/2026-11-15/正常App入口固定配置短验证报告.md)已通过。用户新增后台常驻要求，后续1800秒针对[新resident正常入口](2026-11-15-resident-board-app.md)建立证据；本v3计划的1800秒仍为未测，不以新请求或旧v2长跑补过。

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. 用户已授权多 agent 自主实现；延续当前执行方式，不重新请求方案确认。

**Goal:** 补齐冻结 v3 原生 App 正常厂商启动路径的界面配置、长期回放、资源与正常退出证据。

**Architecture:** 新增独立验证辅助脚本，启动原厂商包装器并读取其私有ready/进程收据。临时uinput键盘操作真实检测页，外部观察器记录API与/proc；不导入或替换生产服务/Pipeline/QML。纯证据核查函数在宿主可测试，实际NPU操作由root串行执行。

**Tech Stack:** Python3.10 stdlib、Linux uinput/ioctl、现有OpenCV/NumPy、正常Qt5/qmlscene/Wayland。

**Spec:** [正常启动验证设计](../specs/2026-11-15-normal-launcher-validation.md)。

## Global Constraints

- 冻结v3源码/模型/QML/ZIP/App2MD和图标目标；所有新文件在新验证目录，不覆盖旧证据。
- 正常厂商wrapper→launch.py→service→Main.qml，禁止替换私有wrapper、Pipeline factory或SDK。
- 模型helmet_416x416.rknn、输入construction-replay.avi、responsive、conf.35/NMS.45/skip0/TTL1；非物理相机。
- 配置/开始来自真实页面按键；API禁止start与WiFi写，仅GET和结束/故障POST空stop。
- token不进入日志/argv/env/导出；设备/进程操作仅本次拥有者，不外杀SDK线程/服务。
- 一次一个NPU任务，normal stop与生命周期核验；进程仍在时不重启。
- 正式观测时长下界≥1800秒；保守FPS计数差/时长上界；不冒称完整逐帧库存或连续显示/无泄漏。

## Review Focus

- 错误会话、PID复用或launch未ready时输入必须拒绝；测试进程start token绑定。
- 未授权或失败截图工具即使exit0，也不得成为渲染成功证据；缺图/不可解码测试。
- HTTP时序/计数基线不准不得虚增30分钟或10FPS；计数退步、非有限时刻、边界时间测试。
- 键盘按住/异常失败必须释放并销毁本任务设备，不误发到其它界面；白名单与短写失败测试。
- 真实业务错误、超时或清理未终态不许被exit0掩盖；失败清单/身份变化/私密材料残留测试。

## Task 1 — 验证辅助脚本与可测试证据核查

**Files:**
- Create: `docs/experiments/2026-11-15/native_keyboard.py`：白名单临时键盘，输入事件结构与拥有者检查。
- Create: `docs/experiments/2026-11-15/verify_normal_launcher.py`：probe/run/verify与独立观察记录。
- Create: `tests/test_normal_launcher_verification.py`：真实纯函数与受控设备/HTTP边界行为。
- Create: 当前毕业计划SDD目录内`normal-launcher-brief.md`、`normal-launcher-report.md`及红绿日志；root维护计划/ledger和diff。

**Interfaces:**
- `VirtualKeyboard`上下文管理器、`press(key)`、`chord(modifier,key)`；设备创建/销毁为Linux限定，事件编码/白名单验证宿主可测。
- `validate_interval(first,last)`接受两条带`request_started`/`response_finished`/`detection.counters`的API观察，返回时间上下界、计数差、保守FPS；不接受不完整、倒退或NaN证据。
- CLI `probe --run-id ID --tabs N --smoke-seconds 8`：不启动NPU，仅正常打开检测页、有限导航、App原生截图/退出并核验。
- CLI `run --run-id ID --duration 1800 --keys KEYS_JSON`：仅在root实际probe确认按键后执行固定配置长期观测。
- CLI `verify DIRECTORY`：宿主只读核查终态、原始状态/HTTP/资源与截图，输出独立JSON；不触板或重启任务。

- [x] TDD：先写并运行有意义的失败测试，再实现时间区间、计数、输入白名单/事件边界、拥有者与终态验证；保留实际红/绿日志。
- [x] 实现probe和run；默认不运行board/NPU，参数严格验证，输出新目录，token红线落实。资源/状态采样不足与未测条件显式失败或未验。
- [x] 定向测试通过后运行主树与legacy各一次，真实skip/所有失败写报告；不为无改动重复跑套件。
- [x] 自审并报告DONE或具体concerns；root生成新文件diff交独立review，未通过前不启动正式NPU长跑。N2a49项/零skip、独立PASS/APPROVED另有记录。

## Task 2 — 实际板端导航探查、短配置和长期观测

**Owner:** root；辅助脚本作者不得运行board/NPU或委派。

- [x] 核验helper源码SHA、现v3全28文件、输入视频与wrapper目标，确认无活跃App/NPU。28项是开跑前单独preflight，短run自身记录24项，两个证据范围分开。
- [x] 在新ID下先做不启动检测的Tab截图probe，实际看图确定三个下拉框和开始按钮的按键顺序。后续N2a安全按键probe5/6明确实际选择，probe4失败保留。
- [x] 短正式run：真实页面选择固定配置、实际APIselection及NPU/JPEG、原生截图、退出清理；失败原样归档，问题闭合后才长跑。normal-v3-short-1约50秒、固定10门槛通过，具体呈现帧限制保留。
- [ ] 新ID正式1800秒观测，只观察同一runner/launcher/service/Qt进程，先终态再复制证据；不能因观测超时重启。
- [ ] 宿主verify和独立实际证据审查，资源描述/原始计数/timebounds复核，保留成功和失败。

## Task 3 — 外部证据和开放门槛记录

- [ ] 在新的实验报告中记录配置、正常入口、有界遥测/额外观察负载、时长边界、各计数/FPS、HTTP/截图/资源、正常退出与未验条件。
- [ ] 仅更新外部阶段总结/计划/ledger；不重打冻结v3包、不改原v2长跑或PPT/HTML，除非实际结论需要后续资料同步。
