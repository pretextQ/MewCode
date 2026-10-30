# 借鉴 Tianshu Harness 的内核监督闭环（原始调研与实施状态）

2026-10-30 实施更新：已按 [08 号授权计划](08-next-stage-plan.md)实现纯 Python 最小监督内核，默认关闭。范围仅 §2.1 的本项目独立实现及交互/headless、服务直跑/容器接线；§2.2–2.5 detector 未实施。选择、实际送达、TTL 和 expect 的本项目语义见[监督指南](../../guides/supervision.md)，验证见[新增验收](../next-stage-2026-10-30.md)。没有照搬上游代码、阈值或引入 TS 依赖，没有监督效果提升证据。

以下为原始调研记录，其“待立项”不代表当前首期状态。

记录日期：2026-10-27。
状态：2026-10-27 已对照 GitHub 源码完成静态审阅并修订，待立项。本文件是调研记录，不代表功能已实现，
也不构成实施授权；维护者审阅后再决定是否立项与排期。

来源与方法：初稿基于 `D:\projects\Tianshu-harness` 的静态文件阅读
（v3.37.0，Apache-2.0，Node.js/TypeScript）。本次审阅从 GitHub 下载并逐文件
比对两份笔记引用的 16 个文件，均与本地快照一致；上游固定基线为
[`23afee4733fdce04abf82e1e3fcc46d6246b9f45`](https://github.com/huiliyi37/Tianshu-harness/tree/23afee4733fdce04abf82e1e3fcc46d6246b9f45)。
未运行上游代码或安装依赖。下文分别说明上游行为与 MewCode 拟议适配；
静态核对不能证明运行效果，实施前仍须验证实际接线与启用条件。

## 1. 结论摘要

- MewCode 当前缺的不是功能，而是**内核内的行为监督闭环**：
  现有 `mewcode/hooks/` 是给用户配置的 event→action 钩子；
  缺少内核自带的"持续观测模型行为 → 注入引导 → 核销是否采纳"的机制。
- 拟议实现维持 **纯 Python**，复用 uv、ruff、mypy、pytest 工具链。
  §3 的约 1,600 行仅是所选上游模块的初步规模记录，不包含完整依赖与接线成本，
  不能据此承诺一周交付。嵌入 TS 运行时会增加 Node 工具链与跨进程维护成本，首期不采用。
- 与 [05 号计划](05-pr-review-improvements.md) 正交：P1-A 是事后静态审查
  最终 diff；监督层是事中持续纠偏，减少坏结果产生，且对交互模式同样生效。
- 许可证：上游 Apache-2.0。拟依据设计目标独立实现 Python 版本；若后续复制、
  改编或翻译上游代码，须另行核对许可证、NOTICE 与署名要求，不能仅凭换语言判断义务。

## 2. 借鉴清单（按预估 ROI 排序）

### 2.1 AdvisoryBus（架构种子，优先级最高）

Tianshu 实现：`src/agent/advisory-bus.ts`（约 760 行实际代码）+
`advisory-readback.ts`（约 375 行）+ `advisory-expectation.ts` /
`advisory-render.ts`（合计约 30 行）。

上游行为（不能把各通道的预算混为一谈）：

- 提醒可经 bus 附录、system-reminder 或状态区投递，并非全部写入 system-reminder；
- 条目字段：去重键 key、优先级（0–1）、分类、优先层
  （constitutional 不截断 / operational 参与 Top-N / informational 填空）、
  TTL（轮次，默认 1）、投递通道（bus 附录 / system-reminder / 仅状态区）；
- 按 key 去重；常态下 bus 渲染的非豁免条目预算为 3，节流时可降为 1；
  constitutional 与 immediate 有豁免，system-reminder 在 Top-N 前分流，状态区也有独立路径。
  因此该数字不是所有通道合计的提醒上限；
- **可选 expect 谓词**：有可观察行为签名的提醒可声明采纳条件
  （如 `tool_appears(..., withinTurns=2)`、`verify_attempted`、
  `pattern_absent`），由 readback 在 postTurn 对照工具事件核销
  adopted/ignored。无 expect 的提醒（如 Intent Anchor）只统计送达，不能判为 ignored。
  行为符合谓词不等于验证通过，也不能单凭采纳率证明提醒带来了效果；
- system-reminder 出队不等于模型已收到，需在实际注入后确认送达，再进入核销账本。

MewCode 现状与差距：`mewcode/hooks/engine.py` 的 `_prompt_messages`
（按 owner 分桶的 prompt 队列）可充当 bus 投递通道，但无 tier/TTL/去重
过滤层，更无核销（adopted/ignored 统计）。

落地要点：新增 `mewcode/supervision/`（与用户配置的 hooks 分离），
普通 Python 函数接入交互与 headless 两条 Agent 循环；首期须明确各通道限额、
跨通道去重、TTL 递减时点及实际送达确认方式，再接入各 detector。

验收要求：覆盖各通道预算与豁免、去重、TTL 过期、实际送达确认；
被裁掉或尚未注入的条目不得计为 ignored。有 expect 时区分等待、采纳与到期未采纳，
无 expect 时只计送达；总线关闭时行为与现状一致。

### 2.2 Self-Verify（未验证就下结论检测）

Tianshu 实现：`src/agent/hooks/self-verify-hook.ts`（约 128 行实际代码）。

上游行为：观察最近约 5 次工具调用的滑动窗口；在只有读/写类工具、
没有被识别为验证的调用等条件下提交 TTL=1 的提醒。实现没有直接识别自然语言中的
“宣布完成”。验证识别还包括 run_tests、deliver_task、具有验证意图的 bash，
以及部分浏览器/可访问性快照取证；文档读取本身不抵消测试或类型检查义务。
实际触发还受首轮抑制、活跃修复、已有高风险义务及只读建议模式等条件影响。

MewCode 现状与差距：服务模式有 `execution.py` 的末端验证，交互模式尚无
同类内置监督检测。该检测只能提供行为线索，不能作为完成声明真实性的判定器。

拟议适配：工具分类与环形缓冲窗口须结合任务性质、改动及未完成的验证义务。
正常只读阅读或事实核查应保留对应证据，不要求运行无关测试；有改动的编码任务
才按已授权范围提醒定向验证。对已尝试但失败的验证应记录失败，不能误报为未尝试。
分类可参考上游 `verification-intent.ts`（约 133 行）的语义：
按命令词面判定 test/typecheck 意图，**尝试 ≠ 通过**（verificationAttempted
永远不作为通过证据）。

验收要求：改了代码但未执行适用验证时触发；正常只读文档任务不触发无关测试要求；
已有适用验证时不重复催促；失败验证不计通过；未知工具与不同验证范围有明确处理规则。

### 2.3 死胡同检测 + 失败指纹（首期提供重新诊断提醒）

Tianshu 实现：`src/agent/hooks/dead-end-detector.ts`（约 115 行实际代码）。

上游行为：

- 信号：同一文件 edit → 验证失败 → 再 edit，循环 ≥3 次且中间无 verify pass
  属于需要重新诊断的信号，不能证明所有同报错调试都无进展；
- **失败指纹**：解析 node:test 格式的前三个失败块（测试名与断言/错误首行），
  做 SHA-256 并取前 8 位 hex；无法识别时返回 null，不能处理任意测试输出。
  两次指纹均有效且变化时，循环数重置为 1；无有效指纹时仍会累计循环数；
- 排除 timeout/env_missing 类失败；测试文件的 TDD 豁免仅在工具为 run_tests 时生效，
  并非任意 bash 测试命令都豁免。阈值由 2 调到 3 是上游降低误报的调整；
- 触发后提交诊断提醒和可选义务/信息素记录；文案建议 read/grep/git diff 等动作，
  expect 实际按指定诊断工具集合观察行为，不据此直接终止 run。

MewCode 现状与差距：服务模式只按 `max_fix_attempts` 次数熔断，
区分不了"在逼近"与"在原地打转"。

拟议适配：先向 `execution.py` 的修复反馈加入重新诊断提醒和结构化证据，
保留现有尝试次数、预算与超时边界，不因相同指纹直接提前 escalate。
为 pytest 和实际支持的测试命令定义指纹提取、动态路径/耗时归一化、未知格式回退，
同时保留原始输出；未知指纹不得表述为“同一失败已被确认”。
若以后引入自动终止，应单独评估误终止率及继续诊断的机会。
上游三次文件级循环不等于服务三次完整 attempt；默认次数相同也不能支持节省 token 的结论。

验收要求：同类失败触发诊断提醒并保留指纹与原始证据；格式无法识别、
仅动态路径变化、失败逐步变化、TDD、环境故障等有独立用例；提醒不自行终止任务。

### 2.4 预算预警（先于熔断）

Tianshu 实现：`src/agent/hooks/turn-budget-hook.ts`（约 39 行实际代码）。

上游行为：针对 maxTurns 轮数预算；剩余进入危险区
（剩余 ≤ max(3, ceil(上限×10%))）时注入一次预警，引导"先验证并交付
已完成部分、未竟工作落盘，不开新支线"；每 run 一次，新 run 重新武装。

MewCode 现状与差距：Agent 已有 max_iterations 限制；服务的 token 预算是另一种资源约束。
直跑模式可中途抛出 `TokenBudgetExceeded` 并 escalate，沙箱模式目前在容器结束后
读取汇总用量，再做预算门禁；不能在宿主直接实现容器内的实时预警。
中断意味着未完成验证/发布，不等于已写入 worktree 的工作必然丢失。

拟议适配：分别设计轮数预警与 token 预警。轮数由 Agent 循环持有；token 阈值
80% 只是待评估提案，并非上游轮数规则。沙箱路径须向容器传入本次剩余 token 预算，
由容器内 Agent 读取实时用量并注入提醒，或先建设有界事件回传通道。
预算传递需考虑历史 attempt 用量、仓库策略与 0 表示不限的语义。

验收要求：区分轮数与 token 预算；覆盖直跑/沙箱、跨 attempt 累计、不限额与新 run 重置；
每 run 只预警一次。现有硬边界保持有效，沙箱未接入实时能力时如实标记未支持。

### 2.5 Intent Anchor（长任务防漂移）

Tianshu 实现：`src/agent/hooks/intent-anchor-hook.ts`（约 42 行实际代码）。

设计语义：自治 run 超过 N 轮（上游默认 20，可配）且距上次用户输入
超过 M 轮（上游默认 10）时，把"本次 run 的启动意图"（截 500 字）
重新注入一次提醒，带冷却（上游 10 轮）。

MewCode 现状与差距：服务模式 job 正是"几十轮无用户输入的长自治 run"，
目标应为"告警 + SOP 目标"（execution.py 组装的上下文），漂移风险最高。

拟议适配：当前 JobStore 审计回调仅筛选工具调用、用量与部分生命周期事件，
没有 run 轮数或最后用户输入轮，无法直接从日志可靠还原。
须在 Agent 内增加 run_id、run_turn、last_user_input_turn 状态，启动与用户 steer 时更新，
同时接入交互/headless 循环；容器内独立维护状态，需复盘时再输出结构化审计事件。
明确 run、attempt、job 的边界与恢复时重置规则，不用工具次数代替模型轮数。

验收要求：短任务不触发；长任务在满足条件后按冷却间隔允许再次触发；
用户新输入刷新锚点/陈旧计时，新 run 正确重置；意图文案取自原始任务目标或告警，
不采用模型转述。无 expect 时仅计送达。

### 2.6 工程过程借鉴（不涉及代码）

- `docs/engineering-metrics.md` 的**可复现指标口径**：区分"静态声明 /
  执行态"两种计数、主动写明复现命令与误差范围。README 中成功率、
  中位耗时等对外数字按此口径书写更经得起质疑。
- `docs/known-issues/` 一篇一问题 + 开放 issue 逐条对代码的审计表
  （其自我约束："引用 ≠ 修完，跑绿 ≠ 功能验收"）。

## 3. 规模与实现方式依据

上游待借鉴模块行数（去注释的近似实际代码，2026-10-27 快照）：

| 模块 | 实际代码约 |
| --- | --- |
| advisory-bus.ts | 760 行 |
| advisory-readback.ts | 375 行 |
| verification-intent.ts | 133 行 |
| self-verify-hook.ts | 128 行 |
| dead-end-detector.ts | 115 行 |
| intent-anchor-hook.ts | 42 行 |
| turn-budget-hook.ts | 39 行 |
| expectation + render | 31 行 |
| **合计** | **约 1,623 行** |

以上为初稿的去注释近似计数，未保存计数脚本，不作为可复现工程指标。
这些文件还依赖工具分类、证据、只读任务判定、控制信号与运行时接线等模块；
不能将所选文件行数等同于完整移植范围。先定义 Python 最小接口与跨模式接线，
再按分项验收工作估算成本。

## 4. 不建议抄的部分（边界）

- **75 个 hook 全家桶**：上游自己的文档警告"改 hook 前先看 gate 条件，
  勿假设某 hook 一定在跑"。首期候选为一个总线和四类 detector（§2.1–2.5），每个带脱网单测。
- 过度特化模块（stigmergy 信息素、physarum、MCTS planning、council 等）：
  是其自身迭代长出的、有数据闭环支撑才成立的能力，冷启动移植只会是死代码。
- prefix-cache 工程（冻结前缀 / appendixDelta 字节稳定）：为 TUI 长会话
  高频对话设计；服务模式单 job 7.7k–10.8k token 收益小，暂缓。

## 5. 建议的后续顺序（供审阅，非授权）

1. 维护者审阅本文，确认立项与首期范围；
2. 若立项：先落 §2.1 总线（含核销与关闭开关），再挂 §2.2–2.5 各 detector；
3. 服务模式的 §2.3 可先以修复反馈中的诊断提醒独立交付；不引入相同指纹自动终止；
4. 评估方式：新增 detector 各配构造用例 + 脱网单测；效果评估另行建立基线，
   不把现有小样本回放结果当作监督层的效果证据。

拟议目录（实施设计时可再定）：

```
mewcode/supervision/     # 内核纪律，非用户可配置 hooks
  advisory.py            # 总线：tier/TTL/去重/expect
  readback.py            # 核销：adopted / ignored 账本
  detectors.py           # self-verify / dead-end / turn-budget / intent-anchor
```

## 6. 参考资料

- [Tianshu Harness](https://github.com/huiliyi37/Tianshu-harness)（2026-10-27
  本地快照 v3.37.0，Apache-2.0；上游功能与阈值持续演进，实施前重新核对）
- 固定基线源码：[AdvisoryBus](https://github.com/huiliyi37/Tianshu-harness/blob/23afee4733fdce04abf82e1e3fcc46d6246b9f45/src/agent/advisory-bus.ts)、[Readback](https://github.com/huiliyi37/Tianshu-harness/blob/23afee4733fdce04abf82e1e3fcc46d6246b9f45/src/agent/advisory-readback.ts)、[Self-Verify](https://github.com/huiliyi37/Tianshu-harness/blob/23afee4733fdce04abf82e1e3fcc46d6246b9f45/src/agent/hooks/self-verify-hook.ts)、[Dead-End](https://github.com/huiliyi37/Tianshu-harness/blob/23afee4733fdce04abf82e1e3fcc46d6246b9f45/src/agent/hooks/dead-end-detector.ts)、[Turn Budget](https://github.com/huiliyi37/Tianshu-harness/blob/23afee4733fdce04abf82e1e3fcc46d6246b9f45/src/agent/hooks/turn-budget-hook.ts)、[Intent Anchor](https://github.com/huiliyi37/Tianshu-harness/blob/23afee4733fdce04abf82e1e3fcc46d6246b9f45/src/agent/hooks/intent-anchor-hook.ts)。
- 本仓：[借鉴 PR-Agent 的改进计划](05-pr-review-improvements.md)（P1-A/P1-B/P2/P3）
- 本文所有上游阈值（冷却 10 轮、循环 3 次、危险区 10% 等）是上游经验值，
  MewCode 实施时应以自身回放数据重新标定，不直接照抄。
