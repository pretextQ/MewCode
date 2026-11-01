# 服务交接与恢复

服务将告警处理和通知交接分别持久化。通知出错不会启动新的修复，也不会消耗修复重试预算。
当前约定为每个 SQLite 数据库运行一个服务进程、一个通知发送器；不是多副本分布式队列。

## 通知

`received`、`pr_opened`、`human_review`、`escalate` 的通知意图与对应状态/审计事件在同一事务提交。
执行链、Worker 超时/异常及人工决定端点走同一个状态转移入口，均可产生通知。
重复告警、恢复回退不再登记“收到告警”通知。数据库写入失败时整笔事务回滚；网络发送在事务之外进行。

发送器保存成功、重试、配置阻塞、明确跳过的结果。网络错误、限流、服务端错误使用有抖动的退避，
普通退避上限约五分钟，数字形式的 `Retry-After` 最多等待一小时；阻塞项至少一分钟后复查。
钉钉、企微须返回合法的 `errcode: 0`，不能仅凭 HTTP 200 判定成功。
未知业务错误保留为重试，HTTP 鉴权等错误保留为阻塞；修正配置后可重试，无自动丢弃或最大次数清除。

同 Job 的较早待发消息优先，失败 Job 不阻止其他 Job 的消息继续处理。
从等待评审转为返工、合并或升级时，尚未发送的旧 `human_review` 消息标为 `superseded`。
已经开始的 HTTP 请求无法撤回。其他消息保留为历史事件，正文携带发生时间、当时状态及稳定事件序号。

新 Job 在接入时固定渠道指纹；历史 Job 的待办在首次发送前固定。
重试时使用当前配置解析凭据，但指纹不同就阻塞，不把旧消息悄悄转发到新地址。
URL 和凭据不复制进通知记录或策略快照，发送错误仅保留安全摘要。
通知明确关闭时记 `skipped`；已阻塞的旧渠道切换为关闭也须显式接受渠道变化。

远端接收后、本地回执提交前退出，可能重复发送同一个事件；当前 webhook 没有通用远端去重协议，
因此不保证恰好一次。`sent` 表示收到渠道成功响应，不表示人已阅读。

在带鉴权的 `/jobs/{id}/report` 中查看 `notifications`；`event_seq` 是消息标识。
重试失败或阻塞项需要配置服务 token：

```http
POST /jobs/{id}/notifications/{event_seq}/retry
Authorization: Bearer <service-token>
Content-Type: application/json

{}
```

确实需要更换目的地或更新导致指纹变化的凭据时，使用 `{"accept_route_change": true}`。
这项操作仅影响指定消息，写入 `notification_retry_requested` 审计；不会重跑 Job。
已发送、已跳过、已取代和普通待发项不能通过此接口重复发送。

`/metrics` 增加 `mewcode_notifications{status=...}` 与 `mewcode_notification_oldest_pending_seconds`。
可据此监测积压和阻塞。API 鉴权沿用现有服务规则，修改端点必须配置 token。

## 策略

新 Job 原子保存仓库名/路径、严重度准入配置、worktree 基线选择、PR base、预算、渠道指纹及策略哈希。
哈希来自生效配置，不包含明文 webhook URL。无显式 worktree 分支时保留原有 `HEAD` 选择，
快照不是仓库内容或 Git 提交的冻结；实际工作区现场仍由已有 worktree 机制恢复。

执行链入口、每次生产 Agent 调用前和发布入口检查当前策略：

- 仓库路径、worktree 基线或 PR base 改变：升级人工，保留已有工作区和 PR，不自动迁移或改 base。
- 严重度已不允许：升级人工。策略损坏时也不继续修复。
- 预算：取原任务额度与当前额度中更严格的值；0 表示无限额。每次调用仍累计已有消耗。
- 通知：独立检查渠道指纹，失败不改变修复结果。

独立审查也遵守原任务预算；沙箱仍为运行后检查实际用量，不提供容器内实时 token 熔断。
配置变化不会中断已经在途的模型请求或外部动作。检查在阶段边界执行，不声称能原子撤销远端操作。
旧 Job 缺少快照时，在首次进入新执行代码时采纳当前配置，并登记 `policy_snapshot`，不冒称恢复了历史授权。

## 执行记录

一次 Worker 派工对应一条 execution，内部可以有多次修复 attempt。
记录起因（首次、重启恢复、handler 取消后重试、人工返工）、起止时间/状态、运行结果及已落库 token 增量。
`job_events.execution_id` 关联本次派工的状态、模型、验证和发布证据；报告中的 Agent attempt 也携带该 ID。
`completed` 只表示 handler 正常返回，业务结论看 `end_state`，不代表修复成功。

修复失败重试次数保持原状态机语义，人工返工和恢复不额外递增。
重启将未收尾的执行记录标为 `interrupted`，等待人工的 Job 不重新执行。
关停前已排队的修改请求恢复时保留 `changes_requested` 和评审原因。
进程被硬杀时，尚未落库的 provider 用量可能缺失；执行记录不声称能恢复未知消耗。

开启 `service.enable_supervision` 时，修复后验证另存 pytest 失败指纹、分类与连续比较证据，并关联当前 execution/attempt。相同证据只向获准的下一次重试追加重新诊断建议；基线、取消重派、重启恢复、人工返工不沿用旧连续计数。关闭时保留原反馈。pytest 契约、可能 TDD/环境/超时豁免、原始输出截断和 `prepared` 边界见[监督指南](supervision.md#服务-dead-end-反馈2026-11-01)。

## 迁移与范围

启动时增量创建新表，并给旧事件补充默认空的 execution ID；历史 Job、状态、用量和审计保留。
不为历史事件推断是否发过通知，不批量补发历史消息，不伪造旧执行记录。
数据库迁移后如需运行旧代码，应事先备份数据库并评估回退；旧版本不会消费新 Outbox。

本轮借鉴了 [dsh-superagent 的 Effects](https://github.com/deepseek-ai/dsh-superagent-prototype/blob/6337fc823ce20251c8de8085948165c81aaa701e/src/app/effects.ts)
与 [Outbox](https://github.com/deepseek-ai/dsh-superagent-prototype/blob/6337fc823ce20251c8de8085948165c81aaa701e/src/app/outbox.ts) 的事务与回执边界，
实现仍沿用 MewCode 的 Python 服务和既有状态机。PR 发布持久化队列、GitHub 评论/评审接入、全只读 observe 模式仍为后续事项。
