# M1：告警驱动修复

更新：2026-10-30。M1 已完成；当前验收见[报告](../acceptance/acceptance-2026-10-30.md)。

Alertmanager 或手动入口经认证与仓库策略判断，生成告警指纹。JobStore 在同一 SQLite 事务中查重、创建任务或记录重复告警；新建方入队和通知。

工作队列有界并发；每个 job 使用独立 worktree，执行修复与按配置运行的验证，生成提交和 PR，观察 CI 后交付人工。失败验证重试至上限后 escalate；无改动可收敛为 cant_repro。默认 Docker 不可用不静默转宿主。

测试命令可选，CI 也可不存在；不强制每个任务证明“先失败再通过”。跳过的集成验证必须如实标记。交付 PR 不等于合并或生产恢复。

human_review 不参与自动重启重跑，已有 PR 的同指纹告警跨窗口去重。鉴权 POST /jobs/{id}/decision 记录人工 merged、changes_requested 或 escalate，不调用远端合并。

历史真实 PR 演示见[完整历史](../history/evolution-2026-10-05.md)，后续修复见[维护记录](../../guides/maintenance.md)。本次工程套件覆盖链路，但未新建真实远端修复 PR。
