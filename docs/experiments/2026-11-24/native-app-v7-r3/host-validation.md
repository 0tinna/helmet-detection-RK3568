# native-app-v7-r3 主机验证

日期：2026-11-24

状态：`host-pass`。以下结果证明主机代码、HTTP 契约、发布构建和浏览器脚本
行为，不证明实体手机、人工拔线、真实多人事件或物理屏幕观察。

## 1. 发布身份

| 项目 | 值 |
| --- | --- |
| 版本 | `20261124-v7-r3` |
| tag | `native-app-v7-r3` |
| 部署根 | `/userdata/helmet-validation/20261124-native-app-v7-r3` |
| ZIP | `dist/helmet-native-app-20261124-v7-r3.zip` |
| ZIP 字节 | `39257948` |
| ZIP SHA256 | `553667bec091d565b46557f533b546145d2e0e5ae47af0c03506c140ce8d9df8` |
| `code_identity` | `0ab9946fb6c73822ace68c4aaacedbba52ff5adbf6a603b77b45702a7febf0bb` |
| `alert_feature_identity` | `79d2052d3057b93e326e5c95d0db516f17b77ff309ff7116ca46221e5ffb6ffd` |
| 条目 | 45 个，其中 44 个载荷和 1 个内部 `manifest.json` |

内部/外部清单、sidecar、每个载荷哈希、字节数和可执行位一致。发布包不包含
`alert_mail.py`、`mail_probe.py`、`mail.example.json`、`mail.json`、
`event-access.key`、`app-state/`、`results/`、Python 缓存或历史 v7-r1/v7-r2
操作手册。完整检查见[整包核验](release-verification-v7-r3.json)。

与 `pre-link-fix` 包逐文件比较，运行代码、模型、入口和其它载荷均未变化；唯一
变化为 v7-r3 内网看板操作手册中的相对链接。因此早先在相同五文件身份下完成的
真实 UVC 180 秒、常驻和退出证据继续绑定同一条运行代码链，本包不把文档修正
伪装成新的性能或硬件运行。

## 2. 自动化验证

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe -m unittest discover -s helmet/tests -v
.\.venv\Scripts\python.exe -m compileall -q helmet tests docs/tools
git diff --check
```

结果：

- 主测试树 `504` 项，`OK (skipped=15)`。
- `helmet/tests` 共 `25` 项，`OK (skipped=1)`。
- 两棵树合计 `529` 项，`16` 项因 Windows 缺少 Linux/POSIX、符号链接或 NPU
  条件而跳过。
- Python 编译、补丁空白检查和 `helmet/app/deploy.ps1` PowerShell AST 通过。
- 当前运行源码、部署脚本和 v7-r3 构建器的旧 SMTP/邮件/访问密钥扫描为零匹配。

本次提交前的完整输出分别保存在
[主测试树最终日志](regression-host-v7-r3-final.log)和
[legacy 测试树最终日志](regression-legacy-v7-r3-final.log)。

定向测试覆盖直接页面、事件和状态 API、JPEG、旧路径跳转、无 Cookie、只读
`405`、当前状态与历史记录分离、空库真相、筛选分页、媒体失败、清理回执、
五文件身份和 v7-r3 不可覆盖构建。

## 3. 页面和 HTTP 契约

主机通过同一局域网访问 `http://192.168.1.10:8092/`：

| 项目 | 结果 |
| --- | --- |
| `/` | `200 text/html`，无 `Set-Cookie` |
| `/healthz` | `200`，正文 `ok` |
| `/api/status` | `200`，当前检测、告警、存储和 `scope=lan` |
| `/api/events` | `200`，真实空库时返回空数组 |
| `/alerts` | `302 Location: /` |
| `POST /api/events` | `405` |
| CSP | `frame-ancestors 'none'` |
| `X-Content-Type-Options` | `nosniff` |

Edge 实际渲染截图见
[dashboard-host-edge.png](dashboard-host-edge.png)，SHA256
`dd98bbac6f09279975542011247fd70eb6713673091ff938025f6607acf37d09`。
该截图是主机无头浏览器对同一局域网页面的渲染，不是实体手机或板屏人工验收。

## 4. 本版未覆盖

主机测试和浏览器截图不能替代实体手机、真实未戴帽多人事件、人工拔线、人工
屏幕连续性和整机重启。板端已执行的项目与仍未执行的项目分别列在
[板端验证](board-validation.md)。
