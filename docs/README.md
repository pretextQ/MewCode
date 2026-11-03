# MewCode 文档

更新：2026-11-03。使用和配置从[项目 README](../README.md)开始；运行契约在 `guides/`，本目录管理当前状态、设计、验收和历史证据。

## 当前交付

参考库借鉴 **U1–U7 已全部交付**，A1–A5 与 TUI A/B 也已实施，不再作为待开发清单。U1–U7 的最终提交、对应五作业 CI、失败及平台结果集中在[交付台账](acceptance/stage-2026-11-02-borrowing.md)。最终 U7 验收提交为 `51c38c78aa3c565a8ff1bee45242772356aea08a`；后续纯文档提交不替代产品验证。

| 工作线 | 已交付 | 使用与边界 |
| --- | --- | --- |
| CLI/TUI 与 Agent | 共享内核、黑色 TUI、F2 详情、请求配对、队友控制、类型化记忆 | [TUI](../guides/tui.md)、[Agent 契约](../guides/agent-runtime.md)；GUI 终端人工矩阵未完成 |
| 服务 M1–M3 | 告警受理、去重、worktree、隔离修复、PR、人审、策略与指标 | [架构](../guides/architecture.md)；交付 PR 不等于合并或生产恢复 |
| 服务可靠性 | 通知 Outbox、策略快照与重验证、execution 记录 | [服务交接与恢复](../guides/service-reliability.md)；PR 发布队列持久化仍未实现 |
| 独立审查 | 固定提交、覆盖记录、局部输入说明与语义评估 | [独立审查](../guides/independent-review.md)；默认关闭，小样本未证明质量提升 |
| 监督 | 总线、循环预算预警、dead-end、self-verify、intent anchor | [监督指南](../guides/supervision.md)；默认关闭，尚无 token 预算预警或效果结论 |
| U1–U5 | 写保护、目录链/AGENTS.md、诊断、拒绝反馈、编辑 diff | [权限](../guides/permission-semantics.md)、[项目指令](../guides/project-instructions.md)、[诊断](../guides/diagnostics.md) |
| U6–U7 | MCP eager/dispatch、受控只读 Shell 并发与退出码 | [MCP](../guides/mcp-loading.md)、[Agent 契约](../guides/agent-runtime.md)；legacy 默认，native/tool_reference 未验证 |

## 按目的阅读

- 使用、配置和平台行为：[指南索引](../guides/README.md)。
- 开发、检查及隔离约束：[开发与维护](../guides/development.md)。
- 未解决问题、人工验收和未授权候选：[维护待办](backlog.md)。本页是唯一当前待办来源。
- 已交付方案与后续提案：[设计索引](evolution/README.md)。计划中的旧授权与“下一项”只解释当时过程。
- 测试、提交、CI 和真实模型评估：[验收索引](acceptance/README.md)。每份报告保留自己的日期、平台和数字。
- 修复前审查与后续结论：[对照审查](review/README.md)；更早的修复与回放见[历史索引](history/README.md)及[提交映射](../guides/maintenance.md)。

## 接手前必须知道

U1 测试事故曾覆盖真实 `.mewcode/config.yaml`，仍未恢复且没有可靠备份；不能猜补或声称恢复。Linux tree-service 回收 timeout/cancel 曾偶发失败，根因未确认，后续 CI 通过不等于已修复。Windows/Linux GUI 终端人工验收尚未完成。部分临时测试目录和卷仍保留，未经核验不清理。完整边界及证据入口见[维护待办](backlog.md)。

没有新的真实模型预算；不读取真实配置或记忆，不自动启动用户 TUI。Windows/Linux 是目标平台，已确认黑色 TUI；macOS/iTerm2 不作为交付缺口。新任务先核对 Git、暂存区、远端和适用指令，保留并行修改；历史开发授权不自动授权下一项产品能力或本次远端推送。

## 文档维护规则

当前行为写入 `guides/`，当前状态写在本页，待办只更新 `backlog.md`，验收追加到 `acceptance/`，设计保留在 `evolution/`，旧原始记录归 `history/` 或 `review/`。不再维护会话提示堆栈，也不在多个索引复制全量测试数字。

`.gitignore` 按文件或专用目录放行维护文档；原始日志、模型响应、截图、临时仓库等仍为本地证据，不强制加入 Git。标注“本地”的资料不会随普通克隆取得。新增或移动文档须同步入口、相对链接和代码/脚本中的路径引用。整理清单及非 Markdown 资料处置见[本次整理记录](history/documentation-2026-11-03.md)。
