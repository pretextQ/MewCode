# 当前架构补充

更新：2026-11-03。本文保留服务模块职责、事务与预算边界，供源码设计引用；跨入口架构和生命周期见[公开架构](../../guides/architecture.md)，当前结果从[验收索引](../acceptance/README.md)进入。

## 主链路

Alertmanager / 手动接入 → 仓库策略 → SQLite 原子受理与去重 → 有界工作队列 → 独立 worktree → 修复/验证循环 → 固定提交 → 可选独立审查 → PR → CI 观察 → 人工审查。

| 模块 | 职责 |
| --- | --- |
| jobs / runtime / worker | 状态、审计、事务去重、调度、恢复、维护与启停 |
| execution | worktree、修复尝试、用量与验证编排 |
| sandbox / compose | 容器内 Agent 和测试、依赖环境、超时与清理 |
| policy / sop | 目标分支、严重度、预算、通知与规范注入 |
| review / review_diff / review_models / review_prompt / review_render | 固定版本审查、完整证据、输入覆盖、预算、结果校验与展示 |
| publisher / vcs | 提交、推送、结构化 PR 与 CI 门禁 |
| api / metrics / notify | 接入、人工 decision 写回、观测、复盘与通知 |
| processes / shell | 共用进程生命周期与平台命令契约 |

## 已有语义

- 默认并发 3、job 超时 1800 秒、排空 60 秒、去重窗口 1800 秒、修复最多 3 次；以有效配置为准。
- 受理查重、建 job 和审计在同一事务，只有创建者入队；已有 PR 且等待人审的任务跨窗口合并重复告警。
- 重启恢复不自动重跑 human_review；鉴权 decision API 可记录 merged、changes_requested、escalate，不操作远端合并。
- 测试命令可选，CI 可不存在；不强制每次先复现失败再通过。跳过或不存在的验证不等于通过。
- 独立审查发生在本地提交固定后、推送前；默认关闭，仅提供意见。绑定 base/head，完整 diff 与模型输入分开，部分输入不能宣称全量审查。

## 安全与预算

默认沙箱不可用即停止；只有显式关闭或允许 fallback 才走宿主，宿主执行不等于 OS 隔离。DONT_ASK 允许常规操作，显式 deny、危险操作检查与路径约束仍适用。

Docker 网络为 bridge/none，无内置细粒度域名出口白名单。第三方 MCP 的 readOnlyHint 只是声明，不能独立作为安全边界。设置 webhook token 后，除匿名最小健康探针外的服务端点均认证；无 token 仅允许回环监听。

修复预算检查包含历史 attempt 用量：直跑在用量回调检查，沙箱在结果回读后检查，后者不是容器内实时计费硬限制。失败用量已有记账修复；独立审查另有限制，仍受有效 job 预算约束。费用换算不属于服务 costs 端点。

## 后续边界

监督总线、循环预算预警、dead-end、self-verify 与 intent anchor 已实现且默认关闭，契约见[监督指南](../../guides/supervision.md)。更多告警接入器、GitHub review webhook、动态上下文和多轮分块仍为候选，统一见[维护待办](../backlog.md)。多连接受理事务安全不代表多服务实例执行器整体安全。没有生产长期运行与业务收益证据。
