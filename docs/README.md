# MewCode 文档索引

索引更新日期：2026-10-25。以下验证摘要为 2026-10-24 历史记录；当前代码与 CI 状态以 Git 和 GitHub 为准。
当时修复代码基线：`6d457b0`，版本 0.3.0，许可证 MIT。
M1–M3、R1–R5、R01–R09 与本轮 N01–N41 均已完成。
当时验证：1363 passed / 18 skipped；全包 mypy（159 个源文件）、Ruff、依赖审计与四组跨平台 CI 通过。

受版本控制的交接入口见 [维护记录](../guides/maintenance.md)，本地详细记录见 本轮修复完成记录（本地资料：`fixes/review-2026-10-24-fixes.md`）。

## 当前文档

- [PR-Agent 借鉴与后续改进计划](evolution/05-pr-review-improvements.md)：2026-10-25 记录，待实施；优先独立修复审查与覆盖记录。
- 第三轮全量审查记录（本地资料：`fixes/review-2026-10-24-full.md`）：保留原始 03f6687 审查证据；41 项发现已修复，当前状态以维护记录为准。
- 修复总览（本地资料：`fixes/README.md`）：R1–R5 问题、提交和验证结果。
- R1–R5 完成报告（本地资料：`fixes/review-2026-10-08-plan.md`）：修复行为和剩余边界。
- 架构说明（本地资料：`evolution/02-architecture.md`）：按当前实现说明执行链路。
- [演进与验证证据](evolution/README.md)：里程碑、回放数据和指标口径。
- 告警驱动流程（本地资料：`evolution/03-m1-alert-driven.md`）。
- 沙箱、Skills、MCP 与 Compose（本地资料：`evolution/04-m2-enterprise-capability.md`）。
- 项目定位与介绍（本地资料：`evolution/01-positioning.md`）：基于真实实现与实验记录。
- 测试环境（本地资料：`fixes/test-environment.md`）：安全配置和验证方式。
- 待评估事项（本地资料：`fixes/backlog.md`）：未修复候选与已关闭事项。
- [下次会话交接](next-session-prompt.md)。
- 历史档案（本地资料：`history/README.md`）：保留原始修复与演进证据。

## 文档维护原则

本目录默认被 Git 忽略。本次按维护者要求，仅提交后续改进计划、两份索引及会话交接文档；不要强制添加整个目录。
标注为“本地资料”的路径未随本次提交发布；当前公开功能说明仍以仓库 README 和 guides 为准，待实施计划不代表已有功能。

已删除重复的旧阶段计划，原件保留在本地备份中。历史档案中的旧状态和旧命令不代表当前实现。不要在文档中保存 API Key、访问令牌或真实配置内容。
