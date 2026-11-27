# MewCode 文档

更新于 2026-11-27。使用和配置见 [项目 README](../README.md)，运行契约见 [指南索引](../guides/README.md)。

## 当前交付

2026-11-20 核对时，项目版本为 `0.3.0`，文档核对基于 `master` 的 `a1a0c91`。该提交简化了 README 架构图，Windows 和 Ubuntu 的 Python 3.11、3.13 测试矩阵及独立安全回归均通过。[版本声明](../pyproject.toml) · [提交记录](https://github.com/pretextQ/MewCode/commit/a1a0c9105510c841054fb900ae0d546172c85630) · [CI #205](https://github.com/pretextQ/MewCode/actions/runs/44307297888)

11 月 18 日交付了三项服务验证改进。

- **发布版本核对**。服务在单测、适用的集成验证和发布阶段核对 Git 待发布的源码树。发生变化时，服务使验证证据失效并升级人工处理。远端 CI 按实际发布的提交 SHA 查询。[验证与发布版本](../guides/service-reliability.md#本地验证与发布版本)
- **pytest 范围审计**。服务用任务保存的 pytest 基线对照修复结果，发现用例减少、仅收集或新增跳过等变化时提示人工核对。证据不可用时，报告会给出原因。若报告标记 `review_required`，记录人工合并决定时须提供当前发布 SHA 和理由。[范围审计与人工确认](../guides/service-reliability.md#pytest-验证范围)
- **重复失败诊断**。开启服务监督后，连续出现相同且可识别的 pytest 失败时，服务为下一次获准的重试准备结构化诊断。[监督契约](../guides/supervision.md#服务-dead-end-反馈2026-11-01) · [三项改进的验收记录](acceptance/stage-2026-11-18-service-verification.md)

U1–U7、A1–A5 与 TUI A/B 均已交付。U7 最终验收提交为 `51c38c78aa3c565a8ff1bee45242772356aea08a`，各项提交、CI 和平台结果见 [U1–U7 交付台账](acceptance/stage-2026-11-02-borrowing.md)。

11 月 27 日使用脚本模型替身，完成真实 Docker Compose、Redis、GitHub PR 和 CI 的服务验收。验收过程中修正了 Git 忽略目录导致的差异收集失败。[服务真机验收](acceptance/stage-2026-11-27-service-live.md)

| 工作线 | 已交付 | 使用与边界 |
| --- | --- | --- |
| CLI/TUI 与 Agent | 共享内核、黑色 TUI、F2 详情、请求配对、队友控制、类型化记忆、Windows 短启动器 | [TUI](../guides/tui.md) · [Agent 契约](../guides/agent-runtime.md) · [Windows 启动与恢复](../guides/tui.md#windows-终端启动和恢复) |
| 服务 M1–M3 | 告警受理、去重、worktree、隔离修复、PR、人审、策略与指标 | [架构](../guides/architecture.md)；交付 PR 不等于合并或生产恢复 |
| 服务可靠性 | 通知 Outbox、策略快照与重验证、execution 记录 | [服务交接与恢复](../guides/service-reliability.md) |
| 独立审查 | 固定提交、覆盖记录、局部输入说明与语义评估 | [独立审查](../guides/independent-review.md)；默认关闭，小样本未证明质量提升 |
| 监督 | 总线、循环预算预警、dead-end 与 pytest 结构化重试反馈、self-verify、intent anchor | [监督指南](../guides/supervision.md)；默认关闭，尚无 token 预算预警或效果结论 |
| U1–U5 | 写保护、目录链/AGENTS.md、诊断、拒绝反馈、编辑 diff | [权限](../guides/permission-semantics.md)、[项目指令](../guides/project-instructions.md)、[诊断](../guides/diagnostics.md) |
| U6–U7 | MCP eager/dispatch、受控只读 Shell 并发与退出码 | MCP 默认使用 `legacy`，已实现的加载模式见 [MCP 指南](../guides/mcp-loading.md)，Shell 行为见 [Agent 契约](../guides/agent-runtime.md) |

## 按目的阅读

- 使用、配置和平台行为：[指南索引](../guides/README.md)。
- 开发、检查及隔离约束：[开发与维护](../guides/development.md)。
- 当前待办、人工验收和候选方向统一记录在 [维护待办](backlog.md)。
- 已交付方案与后续提案：[设计索引](evolution/README.md)。计划中的旧授权与“下一项”只解释当时过程。
- 测试、提交、CI 和真实模型评估：[验收索引](acceptance/README.md)。每份报告保留自己的日期、平台和数字。
- 修复前审查与后续结论：[对照审查](review/README.md)；更早的修复与回放见[历史索引](history/README.md)及[提交映射](../guides/maintenance.md)。
- 本机保留的日志、快照和运行工件见 [证据索引](evidence/README.md)。

## 接手前必须知道

U1 测试事故曾覆盖真实 `.mewcode/config.yaml`，仍未恢复且没有可靠备份；不能猜补或声称恢复。Linux 进程回收测试已修正存活误判，两次早期 tree-service 失败仍缺少当时的进程状态，详见[专项验收](acceptance/stage-2026-11-27-process-cleanup.md)。Linux XTerm 实窗检查已通过，Windows Terminal 仍有待人工确认的项目。部分临时测试目录和卷仍保留，未经核验不清理。[实窗验收记录](acceptance/stage-2026-11-27-terminal-gui.md) · [维护待办](backlog.md)

没有新的真实模型预算；不读取真实配置或记忆，不自动启动用户 TUI。Windows/Linux 是目标平台，已确认黑色 TUI；macOS/iTerm2 不作为交付缺口。新任务先核对 Git、暂存区、远端和适用指令，保留并行修改；历史开发授权不自动授权下一项产品能力或本次远端推送。

## 本地审查入口

以下为整理前已有的用户入口，原文保留，描述的是当时的审查活动；当前完成状态以本页和交付台账为准。

- 参考库对照审查（本地）：[2026-11-01 上游对照](review/README.md)。只读静态审查，未改动代码、未运行测试；A 类必修 5 条、B 类借鉴 10 条、C 类勿回退 7 项，另有两条待运行时验证。

## 文档维护规则

当前行为写入 `guides/`，当前状态写在本页，待办只更新 `backlog.md`，验收追加到 `acceptance/`，设计保留在 `evolution/`，旧原始记录归 `history/` 或 `review/`。不再维护会话提示堆栈，也不在多个索引复制全量测试数字。

`.gitignore` 按文件或专用目录放行维护文档。原始日志、模型响应、截图和临时仓库等保存在本地。新增或移动文档时同步入口、相对链接和代码或脚本中的路径引用。历史迁移清单和非 Markdown 资料的处理见 [2026-11-03 文档整理记录](history/documentation-2026-11-03.md)。
