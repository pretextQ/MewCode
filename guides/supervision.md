# 监督内核与执行循环预算预警

默认关闭。纯 Python，2026-10-31 新增一个执行循环预算 detector；开启监督后，接近循环上限时会注入提醒，不增加模型请求。现有 hooks 的用户扩展队列、权限和回调语义保留。没有自动终止、返工、GitHub 动作或生产效果承诺。

## 配置与接线

```yaml
# CLI -p / TUI
enable_supervision: false
# mewcode serve（独立开关）
service:
  enable_supervision: false
```

只接受布尔值；顶层键合并时未声明保留已有值。服务段沿用既有整段替换规则；单独声明的显式 false 也能关闭。服务开关传给直跑 Agent，或写入最小容器配置的顶层键，由容器 CLI 同一加载器读取。子代理、teammate、worktree 子代理继承 enabled，各自创建新总线；父提醒与账本不复制。开关在创建 Agent 时读取。

## 数据、预算和生命周期

`mewcode.supervision` 导出 `AdvisoryBus`、`Advisory`、`Expectation`。`Agent.supervision` 是该实例的总线。每条提醒有跨通道唯一 key、priority（0–100，越高越先）、category、channel、TTL 和可选 expect。

| 通道 | 每次最大条数 | 渲染 UTF-8 字节 | 送达语义 |
| --- | --- | --- | --- |
| system | 2 | 2048 | 本次 system 附录 |
| reminder | 3 | 4096（含 system-reminder 外壳） | 本次请求副本的 transient reminder |
| status | 8 | 2048 | 仅结构化 report 的 status 列表，不发模型，不记送达 |

没有优先级/分类豁免。选择先按优先级，再按发布顺序；大于剩余预算的条目跳过，不截断，允许后面的较小条目入选。限额是提醒渲染内容，不是整个 provider JSON 或 token 预算；原上下文/token 硬边界继续有效。Python 集成可在构造总线时指定 limits，各通道独立计量。默认给小项目至多五条模型提醒、约 6 KiB，状态独立；这是本项目的保守资源选择，未经效果校准。

每 run 最多接受 128 个不同 key（完成后也保留去重，达到容量则拒绝新条目），事件环最多 256 项；key/category/text 各有 256/128/16384 字节上限。优先级不突破内存/通道预算。publish 返回是否接受，重复 key 不覆盖旧提醒，也不刷新 TTL。

一个公共 `run` / `run_to_completion` 调用对应一个新 run，UUID 标识。服务的每个修复 attempt 调用新 run；中断恢复也新建，不从磁盘重放提醒。首轮之前已 publish 的条目保留；后续调用清空上个 run 的提醒、去重和轮数。需要保留审计时，在下一 run 前读取 report；CLI JSON 和服务回调会在结束时取出结果。

model turn 是本次决策请求收到首个 provider 响应事件时计一轮；工具调用、自动压缩和记忆请求不计决策轮。每个请求尝试有独立 request ID；首响应前重试不增加 turn。TTL=N 的截止轮为发布时 turn+N；该轮及之前的请求可选择它。首响应才推进 turn，在途旧请求的首响应也会推进，不保证每条获得 N 次发送机会。过期后懒清理标 expired；未送达的过期条目不会 ignored。

`steer()` 将 pending/waiting 标 superseded，记录最后用户输入轮；TUI 新用户输入接入该接口。并发在途请求无法撤回，若之后收到首响应，仍记录已送达事实，但不恢复已 supersede 的 expect。新一次公共 run 调用重置。每个 Agent 串行调用循环；不支持同实例并发运行两个 run。

## 实际送达与核销

在压缩和工具结果裁剪之后，从总线选择提醒，复制最终请求上下文再注入；不进入持久对话、fork 快照或之后的请求。仅出队、渲染、选择均不算送达。收到本次真实请求的第一个响应事件才 confirm；无响应失败/取消属于未确认，即使服务端可能实际接收，也不伪称送达。后续流失败保留已确认送达；取消/失败不提前核销未结束窗口。已有请求重试策略保持不变，每次重试重新选择，confirm 幂等，旧 run 响应不能记入新 run。

无 expect 的条目只变成 delivered。支持两种显式行为签名：

- `Expectation("tool_call", "Check", within_turns=2)`：模型实际返回该工具调用，表示请求尝试该工具。
- `Expectation("tool_success", "Check", within_turns=2)`：该工具经过权限/hooks 门禁并实际执行后 `is_error=False`。失败、被拒绝、未知工具不能采纳该条件。

它们只证明指定可观察事件；工具成功也不自动证明测试覆盖充分或整个任务通过。expect 窗口含送达当轮，window=1 在当轮工具执行机会结束后可核销；匹配为 adopted，已完整耗尽窗口但未匹配为 ignored，窗口尚未结束为 waiting。正常返回仅表示本次接口结束，不代表任务成功。结束/取消时未到期条目保持未解决，不为凑统计提前 ignored。被通道预算裁掉、未注入、失败未确认和 status-only 提醒均不能 ignored。

## 集成与证据

```python
from mewcode.supervision import Advisory, Expectation

# agent 使用 enable_supervision=True 构造；首个 run 开始前发布，或由受信任内核集成在 run 内发布。
agent.supervision.publish(Advisory(
    key="explicit-check", text="执行本任务约定的 Check 工具。",
    expect=Expectation("tool_call", "Check", within_turns=2),
))
await agent.run_to_completion("合成任务")
report = agent.supervision.report()
```

`report()` 包含 run/turn、请求尝试数、last_user_input_turn、关闭状态、状态计数、各条目送达/核销及有界事件环；status 列表受独立预算控制，完整 entries 是审计账本。交互集成可直接读取 report，首期没有新增 TUI 状态控件。

headless 回调结束时产生 `type=supervision`；CLI JSON 仅启用时增加 supervision 字段。容器回读同一字段；服务 job 写入 `agent_supervision` 有界有效 JSON 摘要（run/turn/attempts/closed/counts），不把整个账本截成无效 JSON。普通文本 CLI 不新增输出。容器进程被硬杀时不保证获得最后摘要，不能把摘要缺席计为 ignored。

## 执行循环预算 detector（2026-10-31）

使用现有 `enable_supervision` 开关；服务仍使用独立的 `service.enable_supervision`。两者默认 false。此前开启监督仅启用基础设施，现在也启用本节的内置预警。

Agent 的 `max_iterations` 是执行循环硬上限，不等同于总线的真实响应 turn 数。每次允许进入循环后，用 `remaining = limit - iteration + 1`（包含当前循环）判断是否进入末尾 `ceil(limit / 10)` 次循环，最少一次。默认上限 50 时，从第 46 次循环开始；上限 1 时可在第一次请求携带预警。短任务提前完成则不触发。这个 10% 是保守实现选择，尚未经效果校准。

预警 key 为 `builtin:iteration-budget`，reminder 通道，priority 90，category `iteration_budget`，TTL 为发布时剩余循环数（总线仍按响应 turn 老化）。每 run 最多发布一次；steer 会 supersede 旧提醒，不重新武装该 key；新 run/服务新 attempt 清空去重后重新武装。它遵守全部容量、通道与字节预算，不享受保留槽位或优先级豁免，可能延迟、过期或被拒收。文案只说已进入末尾区间，不把发布时剩余量伪称为延迟送达时的实时余量。

文案建议优先进行适用验证、交付已完成部分，并如实记录失败与未验证工作；不会要求只读任务运行无关测试，也不扩大工具权限。不带 expect，只统计实际送达，不能声称模型已验证或采纳。`iteration_budget_warning` 事件记录发布时 limit/iteration/remaining/threshold；`delivered` 事件仍需请求首个响应确认。服务紧凑审计摘要保留状态计数，完整 detector 事件从 Agent report / CLI JSON 查看。

请求内部重试、上下文溢出恢复不额外产生 detector 发布，不增加外层循环额度；预警不追加“收尾请求”，原硬上限和失败/取消语义保持。它不是 token 预算预警，不能替代服务 token 熔断。交互、headless、服务直跑及容器 Agent 共用同一实现；子代理继承开关，各自按自己的循环上限判断。

self-verify、dead-end、token 预算预警和 intent anchor 仍未实现。基础内核历史验收见[2026-10-30 报告](../docs/next-stage-2026-10-30.md)，本次验收见[2026-10-31 报告](../docs/stage-2026-10-31.md)。
