# 图标启动与后台常驻 App 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development。用户已授权多agent自主实施；本计划继续该授权，不重复请求方案确认。所有步骤按checkbox记录，真实板端/NPU仅root执行。

**Goal:** 使现原生检测App由图标启动，界面隐藏后继续后台检测，再次点击复用运行，并可明确完整退出。

**Architecture:** 新增独立Python resident管理者和私有本机控制通道，沿用现Qt/loopback令牌服务/Pipeline。短命图标客户端负责启动或唤回，后台管理者持续监督同服务与Qt；隐藏停止预览刷新，完整退出走原正常资源清理。

**Tech Stack:** Python3.10 stdlib、Linux setsid/flock/Unix socket、Qt5.15.8/QML、现OpenCV/NumPy/RKNNLite与WPA后端；不新增systemd、浏览器或云依赖。

**Spec:** [常驻App设计](../specs/2026-11-15-resident-board-app.md)。

## Global Constraints

- 板端真实环境是BusyBox init，未安装systemctl；日常启动必须来自现图标wrapper→launch.py。
- UI隐藏与完整退出分开；隐藏/窗口关闭继续当前检测，二次点击共享同实例/selection/generation/NPU所有者。
- HTTP仅127.0.0.1并保留Host/Origin/令牌验证；Unix控制仅有限JSON、同UID/安全权限、大小/超时受限。密码/token不进argv/env/日志/清单。
- 退出请求不等于已退出；SDK/source/检测/WiFi回滚仍有活所有者时保留锁和真实失败，拒绝重开，不外杀设备线程/服务。
- 模型/共享解码/Pipeline/报警保持；class person=裸头、hat=戴帽头部，conf.35/NMS.45/skip0/TTL1、640×480相机请求及replay/responsive保持。
- 手动WiFi默认scan20/command2/connect20/rollback5秒保持；不自动连接或开机开始检测。
- 所有新板端artifact在`/userdata/helmet-validation`，新v4独立root；旧v3ZIP/发布收据/历史根和两份原封包手册字节冻结。只在终态并核验后备份更新本项目拥有图标入口，不重启厂商桌面。
- 一次一个NPU任务；回放/实际相机、采样Qt/人眼连续显示、后台进程/独立供电、软件fixture/真实Linux与精度门槛分别验收。

## Review Focus

- 同时点击/不同图标/不同安装root、陈旧pointer/PID复用/socket软链接不得产生第二实例或给无关进程发控制。
- 管理者或UI意外死亡、服务仍持有SDK时不能把锁释放或client exit0解释为全部终态；下一次点击必须识别活所有者。
- 隐藏与再显示之间的旧preview、不同generation/epoch及默认UI选择不得伪装成当前检测帧/配置。
- 显示与退出请求返回已受理不得冒称实际物理聚焦、触摸或SDK直接释放；退出超时必须保留真实owner与收据。
- 特殊路径/权限/坏JSON/无限消息、Host/Origin/token错误、日志/环境材料泄漏和API未授权写须拒绝；不改变现WiFi安全/rollback语义。

## Task R1 — 常驻进程、界面与服务生命周期

**Owner:** 新独立implementer；root和旧normal helper owner不共同编辑这些文件。

**Files:** 新`helmet/app/resident.py`（必要时拆聚焦通信模块），修改`helmet/app/launch.py`、`service.py`、`qml/Main.qml`及实际部署文件列表；新`tests/test_app_resident.py`并扩展`tests/test_app_service.py`的对应协议回归。原WiFi/模型/Pipeline及旧封包手册不动。

- [ ] 先保存候选baseline与完整brief；定义resident控制消息/状态、安装与PID/start token、service与Qt所有权记录和单实例租约。
- [ ] TDD先复现并发点击、隐藏检测、旧包恢复/实际selection、退出/活worker重开与安全输入的失败，再实现最小常驻管理/客户端/服务接口与QML行为。
- [ ] 图标客户端可退场而后台管理者持续；同图标再次打开复用，两个图标可指定页；“回到桌面”和窗口close均隐藏，“停止检测”与“退出应用”行为明确，隐藏时画面刷新暂停而状态/唤回仍可处理。
- [ ] UI异常退出不终止活检测；完整退出及服务失败走可证明拥有的正常清理，管理者死亡/遗留活服务身份需阻止第二SDK，失败不伪造成功。
- [ ] 保留一次性smoke参数并明确该验证路径范围；增加验证所必需的安全原生QML截图/状态收据时仅在显式验证开关下运行，不改变默认检测遥测/阈值。
- [ ] 运行有意义定向测试；通过后主树/legacy各一次，平台skip明确。补实际Linux无NPU fixture由root执行，不由author触板。
- [ ] 自审/完整报告/源码哈希；root生成baseline diff，独立review符合性和质量，通过后才新板端App/NPU试验。

## Task R2 — 正常图标路径后台实测与独立证据核查

**Owner:** root；必要的独立验证器由另一个受brief约束implementer完成，不改R1生产候选。

- [ ] 在新唯一v4安装root复制候选，核验源码/6模型/输入SHA，确保旧App/NPU空闲与governor；先在无NPUfixture验证Linux锁/socket/独立会话清理。
- [ ] 正常wrapper/launch.py无NPU实测：启动客户端退出，同resident/service/Qt仍活；隐藏/唤回、两个图标复用同PID/start token，当前页与窗口收据正确；完整退出/锁/私密文件清理。
- [ ] 固定416/construction-replay.avi/responsive/.35/.45/skip0/TTL1由真实界面选择开始；NPU/鲜JPEG、隐藏期间计数增长、再打开同generation/实际selection/新鲜图，停止与完整退出均真终态。新run失败保持原档，先短gate闭合。
- [ ] 真实正常resident路径至少1800秒（如实报告HTTP区间上下界/计数差与保守FPS）、独立资源/Qt采样/后台interval和最终cleanup；不把旧v2私有wrapper或v3一次性smoke作为这一gate。
- [ ] 仅lo UP子网络离线短验证，父网络保持；不切真实WiFi。相机实际出现后另做mode/无NPU帧/短检测/恢复/长跑，不把旧ISP节点当camera通过。
- [ ] 原始证据复制仅在任务与相关进程终态后；宿主独立verify、作者外的实际证据review，FPS/资源/帧身份及未验条件完整。

## Task R3 — v4图标迁移、手册与开题材料交接

**Owner:** root协调，文档worker只改分配新v4正文；与R1文件所有权独立。

- [ ] 新`docs/app/板端App后台运行手册-v4.md`及`离线更新与图标安装-v4.md`，描述图标→前台→后台→唤回→停止/退出、相机插入后重开刷新、离线/U盘更新和真实验证边界；旧v3两MD不覆盖。
- [ ] 新版本打包一次、完整清单/ZIP SHA与载荷逐文件核验；失败/旧v1-v3与CLI包全部保留，不重写历史handoff。
- [ ] 终态后安全备份并迁移仅四owned launcher/icon链接和两desktop；主cfgSHA与无关文件保持；当前installed receipt指向实际v4root。
- [ ] 更新阅读导航/完整手册/阶段报告、开题证据映射，必要时重新生成HTML/PPT并按对应artifact skill渲染QA；后台结果/物理camera/触摸/独立供电/WiFi连接/85%明确分开。
- [ ] 单次最终跨模块复核与证据核验；按实际门槛更新原毕业计划/ledger，不因文件交付将S1/S6或人工项勾选。
