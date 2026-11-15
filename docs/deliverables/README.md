# RK3568项目阅读与开题材料

[项目手册](项目手册.html)包含第一次阅读路线、系统关系图和完整操作手册。用浏览器直接打开，左侧目录可跳转，顶部可打印或另存为PDF。

[当前可编辑PowerPoint v7](开题报告-RK3568-v7.pptx)包含12页、6张原生表格和逐页演讲备注，已通过Artifact Tool结构/版式校验并导出全部页面预览。学校指定模板和姓名、学号、学院、指导教师需填写。[HTML开题演示](开题演示.html)支持左右键、按钮、N备注和全屏，正文约8分钟。

最新正文依据[11月15日阶段总结](../experiments/2026-11-15/阶段总结.md)、[v4-r2 Phase 2终验](../experiments/2026-11-15/native-app-usability-v4-phase2-1/report.md)与[原生App30分钟中文报告](../experiments/2026-11-15/原生App30分钟验证报告.md)：v4-r2已完成正式图标、全屏、常驻、双入口复用、UI退出和196.67秒连续检测；历史30分钟回放组合为1800.051066秒、19,806检测/19,805输出、11.0030283/11.0024728FPS。两者条件不同，不能互换。GUI95.6123%为采样比例，长期连续显示、无泄漏、独立质量与现场仍未验。

这两份HTML在本机加载，不需要联网下载字体、脚本或图片。打开原始代码、实验或Markdown引用时，请保留项目原目录；只复制单个HTML仍可阅读正文和进行演示，但引用文件不会随之复制。

[v4-r2 Phase 2终验](../experiments/2026-11-15/native-app-usability-v4-phase2-1/report.md)补正式入口迁移、resident单实例、隐藏期间帧号继续增长、180秒短跑和完整退出。WiFi已有SSID列表与密码键盘证据，真实认证/DHCP/保存仍需现场验收。v1至v6 PowerPoint草稿保留历史，当前使用上方v7链接。

维护正文时先修改[完整手册](../项目蓝图与完整手册.md)、[阅读导航](../阅读导航.md)或[逐页讲稿](../proposal/开题PPT逐页内容与演讲稿.md)，再运行生成脚本：

```powershell
node docs/tools/build_reading_materials.mjs

$env:RUNTIME_NODE_MODULES='<codex-primary-runtime>/dependencies/node/node_modules'
$env:PRESENTATION_SKILL_DIR='<codex-primary-runtime>/presentations/<version>/skills/presentations'
$env:RUNTIME_PYTHON='<codex-primary-runtime>/dependencies/python/python.exe'
& '<codex-primary-runtime>/dependencies/node/bin/node.exe' `
  docs/tools/build_proposal_pptx.mjs --revision=v8

& .runtime/proposal-pptx-v1/inspect_in_powerpoint.ps1 `
  -DeckPath docs/deliverables/开题报告-RK3568-v8.pptx `
  -OutputDirectory .runtime/proposal-pptx-v8/powerpoint-previews-final
```

HTML生成脚本只使用Node内置模块。PPT生成脚本必须使用已安装的Artifact Tool运行时、Presentation skill和可验证中文字体，并选择不存在的revision；它会执行包结构、版式、原生表格和逐页备注校验。PowerPoint QA脚本再以只读方式打开最终文件并导出12页。生成后重新检查页面、实验数值和备注。运行包的离线部署边界见[离线包说明](../experiments/2026-11-14/offline-bundle.md)；阅读材料与板端运行包分别维护。

[当前原生v4-r2运行包](../../dist/helmet-native-app-20261115-v4-r2.zip)与[发布清单](../experiments/2026-11-15/native-app/bundle-manifest-v4-r2.json)已冻结，39,227,461B/35 entries，SHA `14b56543f7aca0b26c61b663770f8bf54729d0ba61a1ac5069f4f1143e084251`。正式根与两个图标入口已核验；systemui重启后的手指操作、独立供电、真实WiFi和长期现场仍待验。416的30分钟性能继续按原v2运行引用。
