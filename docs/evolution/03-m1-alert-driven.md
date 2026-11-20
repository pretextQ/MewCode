# M1：告警驱动修复

M1 已交付。本文记录告警受理、修复和人工交接的基础链路。运行约定与后续变更见 [服务交接与恢复](../../guides/service-reliability.md)。

Alertmanager 或手动入口按服务鉴权配置和仓库策略处理告警。JobStore 在同一 SQLite 事务中查重、创建任务或记录重复告警。新建方负责入队，通知意图与建单一同持久化，由 Outbox 独立发送。

工作队列有界并发；每个 job 使用独立 worktree，执行修复与按配置运行的验证，生成提交和 PR，观察 CI 后交付人工。失败验证重试至上限后 escalate；无改动可收敛为 cant_repro。默认 Docker 不可用不静默转宿主。

测试命令可选，CI 也可不存在；不强制每个任务证明“先失败再通过”。跳过的集成验证必须如实标记。交付 PR 不等于合并或生产恢复。

human_review 不参与自动重启重跑，已有 PR 的同指纹告警跨窗口去重。鉴权 POST /jobs/{id}/decision 记录人工 merged、changes_requested 或 escalate，不调用远端合并。

2026-10-30 的工程验收覆盖服务链路，当时未新建真实远端修复 PR。2026-11-18 的专项补充了发布版本绑定和 pytest 范围审计。早期真实 PR 的样本见回放摘要，逐项演进过程见历史索引中的本地档案。[10 月 30 日验收](../acceptance/acceptance-2026-10-30.md) · [11 月 18 日验收](../acceptance/stage-2026-11-18-service-verification.md) · [回放摘要](../history/replay-2026-10-05.md) · [历史索引](../history/README.md)
