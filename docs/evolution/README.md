# 设计与演进索引

当前交付以[文档入口](../README.md)为准，未实施方向统一见[维护待办](../backlog.md)，实际测试与提交记录见[验收索引](../acceptance/README.md)。本目录保存设计选择和历史方案；旧授权、暂停点和验收提案不作为新任务指令。

## 已交付方案

| 文档 | 实施状态与用途 |
| --- | --- |
| [服务架构补充](02-architecture.md) | 源码引用的模块、事务及预算边界；运行主链见公开架构指南 |
| [M1 告警驱动修复](03-m1-alert-driven.md) | 已完成；人审交付与重复告警语义 |
| [M2 隔离执行与上下文](04-m2-enterprise-capability.md) | 已完成；Docker/Compose、Skills/MCP 及安全边界 |
| [08 审查边界与最小监督](08-next-stage-plan.md) | A/B 已于 2026-10-30 交付；模型候选比较未证明提升 |
| [09 TUI A/B](09-tui-redesign-plan.md) | 已实施，黑色外观已确认；GUI 人工验收仍待完成 |
| [10 A1–A5](10-review-a1-a5-fix-plan.md) | 已逐项交付；不按修复前缺陷再次立项 |
| [11 U1–U7](11-upstream-borrowing-plan.md) | 已全部交付；精确 SHA、CI、事故与平台结果在验收台账 |

## 调研与后续提案

- [05 PR 审查路线](05-pr-review-improvements.md)：固定提交、覆盖记录、局部输入修正已实施；动态检索、分块、GitHub 返工与持续问题跟踪仍为提案。
- [06 监督设计](06-tianshu-supervision-notes.md)：总线、循环预算预警、dead-end、self-verify、intent anchor 已实现；token 预警未实现。上游方案与本仓阈值/生命周期分开理解。
- [07 测试体系](07-tianshu-test-notes.md)：最小架构/规模守卫及四项回植已完成；其余治理建议未因文档存在而获得实施授权。

## 历史证据

[2026-10-05 回放摘要](../history/replay-2026-10-05.md)保留两轮样本、PR 范围和指标口径；完整执行历史仅在本地档案保留。早期 42 项、R1–R5、R01–R09、N01–N41 均已关闭，见[历史索引](../history/README.md)和[维护映射](../../guides/maintenance.md)。本索引不复制各期全量数字。
