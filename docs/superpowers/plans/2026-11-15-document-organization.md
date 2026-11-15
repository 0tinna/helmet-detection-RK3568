# Project Documentation Organization Plan

> **For agentic workers:** Use `superpowers:executing-plans` to complete this document-only task. The user explicitly requires the primary agent to do all work without subagents. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 整理已冻结项目的阅读资料，把当前交付、操作说明、学术材料与历史原稿分开，并证明代码和实验原始文件未变。

**Architecture:** 保留运行源码、生成器、模型、实验目录和发布 ZIP 的原路径与内容。新增交付说明及目录索引；将根目录初步文档、原开题与旧 PPT 移入 `docs/archive/`，同步当前资料的引用。

**Tech Stack:** Markdown、JSON、PowerShell、Git、现有 Node HTML 导出脚本。

**Spec:** 用户 2026-11-15 请求“整理项目解析文件，初步文档已过期，不要修改代码影响项目运行”。

## Global Constraints

- 不改 `helmet/`、`tests/`、`tools_ai/`、模型、依赖、部署脚本或文档生成脚本。
- 不改实验原始目录、发布 ZIP、历史 manifest 和 SHA 记录。
- 归档原稿和旧 PPT 保留原始字节，记录旧/新路径及 SHA256。
- 代码交付已冻结与未完成的现场/独立质量验证分别说明；不新增训练任务，不补写验收通过。
- 当前 PPT v7 保持文件内容，不重新制作演示内容。
- 沿用会话中已授权的 GitHub 保存操作，核验后提交文档变更并同步远端。

## Review Focus

- 原稿中的“55%”“待训练”等旧状态只作为历史原文阅读。
- 当前入口从 GitHub 克隆后可找到；仅本地保存的证据有明确说明。
- 原始报告和清单中的旧路径仍代表当时环境，不通过改写记录来匹配新目录。
- Markdown 链接、HTML 目录和当前 PPT 路径可用。
- 整理后的工作树代码、模型、依赖和发布包 SHA 与整理前完全一致。

---

### Task 1: Baseline And Archive

**Files:** `.runtime/docs-organization-20261115/before.json`、`docs/archive/`。

**Interfaces:** 输入原文件及当前 Git HEAD；输出原文件哈希、新旧位置和保留字节的归档。

- [x] 记录受保护目录与文件 SHA256、待归档原件 SHA 和整理前缺失链接。
- [x] 将旧初步文档、原开题、v3 更新手册和六份旧 PPT 归档。
- [x] 原学校 Word 模板仅本地迁入归档，不添加到公开 Git 历史。
- [x] 记录归档清单，原稿内部链接按原路径基准解释。

### Task 2: Current Documentation Entrance

**Files:** `README.md`、`AGENTS.md`、`docs/README.md`、`docs/阅读导航.md`、`docs/项目交付说明.md`、`docs/项目蓝图与完整手册.md` 及目录索引。

**Interfaces:** 输入现有 v4-r2 和诊断验收证据；输出统一的当前阅读入口与历史索引。

- [x] 精简根 README，将详细历史数字留给报告和完整手册。
- [x] 新增项目交付说明，写明当前版本、成果、哈希和已知限制。
- [x] 更新阅读导航与各资料目录索引，仅保留当前 v7 在演示目录。
- [x] 修正当前资料中的历史路径、过时 Git 状态和旧版指南引用。

### Task 3: Export, Verify And Save

**Files:** `docs/deliverables/项目手册.html`、`docs/资料整理记录.md`、`.runtime/docs-organization-20261115/after.json`。

**Interfaces:** 输入维护正文与哈希基线；输出离线 HTML、核验结果和文档专用 Git 提交。

- [x] 用未修改的现有生成脚本导出 HTML；保留现有 v7 PPT。
- [x] 核对代码/模型/依赖/原始实验/发布包哈希、归档字节和活动文档链接。
- [x] 检查 HTML 首屏、目录与离线资源；记录已存在的本地证据边界。
- [x] 自审全部变更，仅提交资料和归档相关路径，推送并核对远端 SHA。

## Execution Rulings

- 本次为可逆的资料维护，沿用已有工作区并使用文档分支；不启动板端实验或改动运行环境。
- 验证采用文件哈希、链接与渲染检查；不为文档移动新增产品测试或重跑硬件推理。
- 用户明确禁止子代理，最终核对由主代理完成。
