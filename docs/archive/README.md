# 历史资料归档

这里保存已退出当前阅读入口的资料原件。原稿的训练路线、初步完成度、旧默认版本和早期未实现说明只代表历史状态；当前项目请从[交付说明](../项目交付说明.md)或[完整手册](../项目蓝图与完整手册.md)开始。

## 已迁入的原件

| 资料 | 原位置 | 归档原因 |
| --- | --- | --- |
| [毕业设计初步文档](early-proposals/毕业设计-初步文档.md) | 根目录 `毕业设计-初步文档.md` | 2026-11-12 初步进度，含 55% 和旧训练任务，已不适用当前交付 |
| [原开题文本](early-proposals/开题报告.txt) | 根目录 `开题报告.txt` | 原训练路线与初始要求，保留用于范围变更追溯 |
| [v3 更新指南](app-v3/离线更新与图标安装.md) | `docs/app/离线更新与图标安装.md` | 对应旧根/版本，当前维护用 v4-r2 指南 |
| [初版 PPT](presentations/开题报告-RK3568.pptx) | `docs/deliverables/开题报告-RK3568.pptx` | 早期演示稿 |
| [v2 PPT](presentations/开题报告-RK3568-v2.pptx) | `docs/deliverables/开题报告-RK3568-v2.pptx` | 已由 v7 取代 |
| [v3 PPT](presentations/开题报告-RK3568-v3.pptx) | `docs/deliverables/开题报告-RK3568-v3.pptx` | 资料生成中间版本 |
| [v4 PPT](presentations/开题报告-RK3568-v4.pptx) | `docs/deliverables/开题报告-RK3568-v4.pptx` | 资料生成中间版本 |
| [v5 PPT](presentations/开题报告-RK3568-v5.pptx) | `docs/deliverables/开题报告-RK3568-v5.pptx` | 资料生成中间版本 |
| [v6 PPT](presentations/开题报告-RK3568-v6.pptx) | `docs/deliverables/开题报告-RK3568-v6.pptx` | 资料生成中间版本 |

当前 PPT 保留在[交付目录](../deliverables/README.md)，使用 v7。学校个人 Word 模板移入本机 `local-only/`，仍由既有 `.gitignore` 排除，未上传公开仓库。

## 其他历史记录

- [实验与阶段报告](../experiments/README.md)留在原日期/run 目录，原始记录内容和 SHA 不变。
- [2026-11-13 性能报告](../performance/2026-11-13-board-validation.md)留在 `docs/performance/`，代表早期静态处理基线。
- [开发设计与计划](../superpowers/README.md)留在原目录，任务勾选与预计工期均按历史解释。
- v1/v2/v3/v4 旧 ZIP 和原 manifest 不覆盖、不重打包；旧 ZIP 仍仅存本机 `dist/`。

## 原件与链接的处理

归档文件保留原始字节，包括原编码和换行。旧/新路径、文件大小与 SHA256 见[归档清单](archive-manifest.json)；本目录设置独立 Git 属性，避免文本换行转换改变原件。

原 `开题报告.txt` 使用 GBK 编码，直接按 UTF-8 预览会出现乱码；下载后在编辑器中选择“GBK / 简体中文”打开。它的原始换行和行尾空格也保留，维护正文使用 UTF-8。

历史原稿内部的相对路径保留原样，按上表的原位置解释。阅读当前文档使用本页、[资料目录](../README.md)和[当前学术材料](../proposal/README.md)提供的链接。原清单中的路径仍代表记录时位置，不改写历史清单来匹配归档路径。
