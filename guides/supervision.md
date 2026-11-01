# 监督内核与服务失败反馈

默认关闭。纯 Python，现有执行循环预算 detector、2026-11-01 self-verify 与 intent anchor 首期共用内核；开启监督后，满足各自条件时注入建议，不增加模型请求。现有 hooks 的用户扩展队列、权限和回调语义保留。没有自动终止、返工、GitHub 动作或生产效果承诺。

2026-11-01 增加服务 dead-end 反馈：连续识别到相同 pytest 失败证据时，向已获允许的下一次修复追加重新诊断建议。它在服务验证链实现，与内核循环预算 detector 分开；具体契约和限制见下节。

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

`steer(text)` 将 pending/waiting 标 superseded，记录最后用户输入轮，并以明确文本替换 intent anchor；不带文本的 `steer()` 清除目标。TUI 新用户输入接入该接口。并发在途请求无法撤回，若之后收到首响应，仍记录已送达事实，但不恢复已 supersede 的 expect。新一次公共 run 调用重置。每个 Agent 串行调用循环；不支持同实例并发运行两个 run。

## 实际送达与核销

在压缩和工具结果裁剪之后，从总线选择提醒，复制最终请求上下文再注入；不进入持久对话、fork 快照或之后的请求。仅出队、渲染、选择均不算送达。收到本次真实请求的第一个响应事件才 confirm；无响应失败/取消属于未确认，即使服务端可能实际接收，也不伪称送达。后续流失败保留已确认送达；取消/失败不提前核销未结束窗口。已有请求重试策略保持不变，每次重试重新选择，confirm 幂等，旧 run 响应不能记入新 run。

无 expect 的条目只变成 delivered。支持三种显式行为签名：

- `Expectation("tool_call", "Check", within_turns=2)`：模型实际返回该工具调用，表示请求尝试该工具。
- `Expectation("tool_success", "Check", within_turns=2)`：该工具经过权限/hooks 门禁并实际执行后 `is_error=False`。失败、被拒绝、未知工具不能采纳该条件。
- `Expectation("verification_attempt", "Bash", within_turns=2)`：实际调用内置 Bash 且满足下节的验证命令识别契约，失败也算尝试。普通 Bash、被拒绝或参数无效的调用不匹配；不表示验证通过或覆盖充分。

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

token 预算预警仍未实现，intent anchor 的本仓实现见下节。基础内核历史验收见[2026-10-30 报告](../docs/next-stage-2026-10-30.md)，循环预警验收见[2026-10-31 报告](../docs/stage-2026-10-31.md)。

## Intent anchor 首期（2026-11-01）

复用默认关闭的监督开关与公共请求包装，不追加模型请求。累计至少 20 个真实决策响应轮、且距最近用户输入至少 10 轮时，向下一次原额度允许的请求发布任务摘录；最早在第 21 个响应轮确认送达。轮数按请求首个响应事件计，不要求流成功结束；工具次数、循环迭代、压缩/记忆请求、首响应前失败重试都不增加此响应轮数。20/10 是本仓首期实现选择，尚未经模型效果校准。

来源仅为调用方明确提供的任务：`run_to_completion(task)` 使用本次 task；服务直跑和容器使用本 attempt 的完整修复 prompt（告警、SOP、可能的前次失败反馈），不另提取语义目标。TUI 使用本次用户文本，在 @ 文件内容扩展之前截取；后台通知和无文本自动继续没有来源。Python 调用 `run(conversation, intent="明确目标")` 可提供来源；兼容旧 `run(conversation)`，但未提供 intent 时只跳过本 detector，不从历史、压缩摘要、环境、hook、工具结果、邮件箱或模型转述猜测。其他监督功能保留。子代理若通过 headless task 调用，则按自己的任务独立观察，不复制父锚点；未启用监督的 skill fork 不因此启用。

每次只保存前 500 Unicode 字符，非语义摘要；服务长 prompt 的后续 SOP/证据/限制可能不在摘录里，不能将其当完整授权或约束清单。提醒明确标注来源、是否截断与引用性质，文本边界标签做 HTML 转义；包含的告警等外部证据不会因此获得新权限或覆盖后续用户要求。转义不代表解决了任意提示词注入。只读/问答、无 Bash 和 plan mode 也可提醒原目标，不要求无关验证、不改变工具或权限门禁。

key 为 `builtin:intent-anchor:1` 至 `:3`，reminder 通道、priority=40、TTL=2，无 expect。成功发布后冷却 10 响应轮，每 run 最多接受三条，steer 不重置次数或发布冷却。总线容量拒收不花费该次数；已接受但被通道裁掉、过期或被 steer supersede 仍花费次数及冷却。所有现有容量、通道条数和 UTF-8 字节上限有效，没有保留槽位或优先级豁免；提醒可能延迟、过期或最终未送达。发布/选择/渲染都不是 delivered；实际请求首响应才确认。无 expect，不计 adopted/ignored，不把送达当成遵守目标或避免漂移。

`supervision.steer(text)` 替换目标、更新用户输入轮并使旧 pending/waiting 提醒 superseded；空白/省略文本清除目标，防止旧目标继续提示。响应轮门槛与用户静默门槛仍须满足；旧在途请求可能物理送达，但不恢复旧目标。公共 run、服务新 attempt、取消/失败后的恢复重派重新初始化目标、计时与次数，不从磁盘/历史恢复，不补发未完成请求。请求内部重试与上下文溢出恢复使用已保存的明确来源，pending 条目在 TTL 内可重选，不重武装同一 key、不增加请求额度。

完整 Agent report / CLI JSON / 容器回读增加有界 `intent_anchor` 元数据：source、excerpt_chars、truncated、published、last_published_turn；事件 `intent_anchor_suggested` 仅说明成功发布，实际送达仍看 delivered。元数据及事件不重复保存任务正文；完整 entries 中已发布提醒含转义摘录，与其他提醒的审计相同。服务落库继续只留原有紧凑总线摘要/状态计数，不持久化摘录或 detector 元数据；关闭时原输出不变。真实行为验收见[本轮报告](../docs/stage-2026-11-01-intent-anchor.md)，不含真实模型效果或生产收益结论。

## Self-verify 首期（2026-11-01）

复用上述默认关闭的监督开关。交互、headless、服务直跑及容器 Agent 共用实现；子代理按自己的 run 独立观察。服务末端测试和 dead-end 反馈仍是独立证据链，不用模型内部验证信号替代服务验证或人工审查。

只有实际通过权限、hooks、参数验证并成功返回的**内置** EditFile/WriteFile，对大小写不敏感的 `.py`/`.pyi` 路径才增加观察 revision。revision 是本 run 的成功编辑调用计数，不是 Git 提交或字节 diff；成功的相同内容写入也可能触发。只读、问答、纯文档和其他语言任务不产生编辑义务；已有 dirty worktree、Bash/第三方工具/子代理的改动、失败写入后的部分变化不盘点。仅按工具名称冒充的自定义工具不纳入。

在下一次原预算允许的决策请求、压缩和裁剪之后，若有编辑后尚未识别到验证尝试，且当前不是 plan mode、注册表中存在启用的内置 Bash，才发布 `builtin:self-verify`。不会检测自然语言“完成”或事后追加请求；最后一次循环才编辑时可以没有提醒。无测试或没有适用可运行检查的任务，建议要求说明替代证据和限制，不强制执行无关测试、不自动选命令、不授予权限。是否获权限和是否实际执行仍走原门禁。

识别契约仅为直接 `pytest[.exe]`、`mypy[.exe]`、`ruff[.exe] check`，或 `python[3[.版本]][.exe] -m pytest/mypy/ruff check`；可有普通参数和带引号的可执行路径，允许无选项的字面 `uv run` 包装。拒绝 shell 组合、重定向、换行、变量/命令替换等语法以及帮助、版本、仅收集/列举、ruff 自动修复。`uv run --locked pytest`、`py -m pytest`、npm、任意脚本和其他包装器均未知；未知调用不会解除催促。该识别只说明调用意图，不解析输出、不推断 cwd、目标文件或命令真正覆盖的范围。

工具真实进入 execute 前记录 `verification_attempt`；超时、异常、环境缺失或测试失败均已尝试，不误报为未尝试。完成后独立记录 `verification_result.tool_success`（`is_error=False`），不等于测试语义通过。后一次失败不抹掉早先成功信号；事件保留先后关系，`successful_revision` 仅为最近成功工具信号所关联的最大 revision。命令开始前的 revision 关联这次观察，后续编辑不能借用旧验证；`coverage` 始终 `unknown`。一次受支持尝试会抑制当前 revision 的催促，即便失败、目标无关或覆盖很窄；这是减少催促的行为策略，不是质量义务已满足。

每 run 最多接受一条建议，reminder 通道、priority 60、TTL=2，expect 为上述 `verification_attempt/Bash`，窗口含送达当轮的两轮。服从总线 128 key 容量及所有通道/字节限额，不占保留槽位，可能延迟、过期或拒收；后续编辑和 steer 都不重新武装已使用的 key。尚未送达时若已尝试验证或 Bash 不再可用，pending 条目标 superseded，避免陈旧催促。实际送达依然只由请求首响应确认；adopted 只表示匹配到一次验证尝试，不能叫作验证成功。

steer 暂停旧编辑义务并 supersede 原 pending/waiting 提醒；后续新编辑才恢复观察。在途旧工具结果不能恢复旧义务；在途请求仍可能确认已发生的物理送达，不恢复核销。取消/失败不补发或提前核销；新公共 run、服务新 attempt、恢复/重派均清空观察，不从旧历史或磁盘恢复计数。停止或硬杀后无法保证取得最后摘要，也不伪称验证成功。

完整 Agent report / CLI JSON / 容器回读报告中新增有界 `self_verify`：revision、attempted_revision、successful_revision、suspended、coverage；事件仍在最多 256 项环中，不另存源码、路径、命令或原始测试输出。服务落库仍只保存现有紧凑摘要（含总线状态计数），不持久化完整 detector 事件或 revision。关闭时保持原输出和执行行为。验收见[本轮报告](../docs/stage-2026-11-01-self-verify.md)，没有真实模型效果或生产收益结论。

## 服务 dead-end 反馈（2026-11-01）

只由 `service.enable_supervision` 启用，默认 false；顶层开关不启用服务反馈。每次进入服务执行链时读取一次，连续计数只活在本次 execution 内。关闭时不提取新指纹、不登记反馈事件、不追加提示；已有原始测试证据、有界重试和 Agent 开关接线保持。直跑和容器接收同一服务生成的修复提示，本轮未改变容器配置协议。交互 CLI/TUI、模型内部 Bash 测试、远端 CI 和 Compose 集成测试输出没有接入此指纹比较。

支持的契约 `pytest-long-v1`：直接调用 `pytest[.exe]` 或 `python[版本][.exe] -m pytest`（允许带引号的可执行路径及普通参数），不识别 shell 组合、重定向、`uv run` 等包装命令。测试结果须 exit 1，包含完整 `FAILURES`、`short test summary info`、最终英文 `N failed ... in Xs`，每个失败都有下划线标题、`E` 诊断行与唯一完整 `FAILED 文件::测试` 标识，块数、标识数和最终失败数一致。默认 pytest 普通 traceback 已通过真实子进程验证；`--tb=no`、缺失 summary/诊断块、被截断的节点、混合 collection error、插件改写导致不满足契约的输出均为 unknown。不能以任意输出的哈希冒称识别了失败。

指纹为全部失败节点、错误摘要及全部失败块的完整 SHA-256，不只取前三个，也不只散列已展示的 excerpt。节点与块各自排序，块保留标题与源码、断言、错误值、行号、捕获输出。归一化仅去除 ANSI 控制格式、CRLF/行尾空白，把已知 worktree 根换为占位符，并把 pytest 管理的 `.../pytest-of-用户/pytest-数字` 根换为占位符；保留后续目录/文件名及数字。pytest 缩进或 `>` 开头的源码行保留路径字面量。session/progress 不进入失败块，标准 warnings/durations 等后续 section 和最终耗时不进入指纹。任意 `/tmp` 路径、随机值、时间戳、对象地址及捕获日志不做通用删除；这些内容变化仍可能使证据变化，不能据此证明修复取得进展。

结构化分类包括 pytest_failure、passed、not_run、unknown、timeout、environment、infrastructure。runner 超时或已识别的 pytest timeout 信号单列；命令不可用、导入/依赖错误为 environment（文本信号仅表示疑似环境问题），pytest 内部错误/用法错误 exit 3/4 为 infrastructure。其他退出与未知格式诚实回退。它们均中断连续比较，但不会改变原状态机对故障的处理。

worktree 相对 HEAD 的改动清单包含 `tests/`、`test_*.py`、`*_test.py`、`test.py`、`tests.py` 或 `conftest.py` 时，记录 `possible_tdd=true`，保留可识别指纹但暂停比较并清空连续计数。这是保守的测试修改信号，不能推断开发者确实在做 TDD；已修改的测试文件在本 execution 后续尝试中仍豁免。非标准测试文件命名无法自动识别。

只比较同一次 execution 中连续的修复后验证：首次为 first，指纹变化为 changed，相同为 same；基线测试不计入。两次连续相同证据后，若现有重试上限仍允许下一次修复，向原失败反馈尾部追加标明服务来源的重新诊断建议。默认三次完整修复 attempt 时，最早第三次携带建议。达到硬上限仍照常升级，不准备不存在的下一次提醒；不会新增 attempt、模型请求或预算，也不改变工具权限。上游三次文件 edit 循环与这里两次完整验证比较是不同信号，不能据默认次数声称节省成本。

`/jobs/{id}/report` 的 `verification.feedback` 展示 `verification_feedback` 和 `dead_end_reminder`，携带 event_seq、execution_id、attempt、分类、指纹、关系、连续数、possible_tdd 与最多 400 字符的 normalized_excerpt（附截断标记）。`raw_event_seq` 指向原 `verify_tests`：仍遵循原有最多尾部 2000 字符、整条摘要最多 4000 字符约定，未新增原始输出全量落盘。哈希使用截取前完整输出；证据同时标注输出长度与原始保留是否截断。测试、集成和 CI 报告条目也展示原事件序号与 execution ID。

提醒事件的 `prepared` 仅表示已组装进下一次服务反馈，不表示真实 provider 已接收、模型已采纳或重新诊断成功。这里没有总线 expect 核销，也不借用内核 delivered 指标。取消后重派、重启恢复、人工返工各建新比较器，旧证据继续保留在旧 execution 下，不重放旧提醒或连续数；原 attempts 和预算累计语义保留。验收见[本轮报告](../docs/stage-2026-11-01-dead-end.md)，目前只有行为验证，没有生产收益或模型质量提升结论。
