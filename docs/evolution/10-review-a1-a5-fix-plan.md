# A1–A5 修复实施计划（2026-11-02）

## 授权、基线与交付

用户已明确要求“规划好这些修改方案，并开启新会话来修复，每完成一个就提交推送一次”。本文件是新会话的执行计划，授权完成以下五项修复、必要测试和文档更新，并逐项提交推送当前 master。无需逐项重新请求批准；不扩展到 B/C/V 项、真实模型付费评测、生产部署或自动合并。

起点为 `0d2e2130525779b250baeef712dcc5f7a8d37d4c`，此前 Windows/Linux × Python 3.11/3.13 与安全五个 CI 作业成功。新会话开工须重新核对实时 Git/远端及适用 AGENTS.md，不把历史绿灯当成新代码验收。原有 `docs/README.md` 一条“参考库对照审查（本地）”改动为用户已有工作，继续留工作区，不夹带提交。本次规划新增的文件、白名单和文档入口可纳入第一项修复的提交，避免额外混杂的规划提交；提交暂存索引须遵守仓库 LF 规范，不能直接 hash 未规范化的 Windows CRLF 内容。

本地核实档案：`docs/review/verification-2026-11-02.md`、`docs/review/evidence-2026-11-02/verify_a1_a5.py` 和 observations.json。它们被忽略，不强制添加整个目录。普通克隆看不到的证据不得变成唯一维护入口；每项新增正式回归测试和公开指南。旧复现脚本目前断言缺陷存在，修复后它失败是预期，不能用其“observations verified”充当修复通过。

**执行顺序：A5 → A3 → A1 → A2 → A4。**逐项完成设计、红绿验证、文档、提交推送及 CI，确认上一项 CI 成功再进入下一项产品修改；不得把五项积攒成一个提交。

每项收尾：

1. 新增能在修改前失败的回归用例，修复后通过；已存在的同义测试不重复堆叠。保留失败/成功日志，测试使用临时 cwd/home、脚本 LLM 或 SDK 替身，不读取真实配置/记忆，不调用付费模型。
2. 运行专项测试、Ruff、Windows/Linux 目标全包 mypy、工程守卫、锁文件一致性及 diff 检查。共享 Agent/请求/生命周期变更运行全量 pytest；Docker/MCP 专项仅在实际跨过相关边界时追加。遵守现有文件行数预算，必要时抽模块，不提高 ceiling。
3. 更新功能指南、阶段验收与工作入口；分清工具成功、任务完成、请求合法、真实模型效果，不宣称未经测量的收益。
4. 逐文件/逐块暂存本项代码、测试及文档，检查 `git diff --cached --check` 和 diff，核实无凭据、原始配置或其他未授权改动。提交信息英文并包含缺陷 ID。
5. 正常推送 origin/master（不强推、不覆盖远端新增提交），核对本地/远端 SHA，通过 GitHub 工具核实该 SHA 的五个 CI 作业。失败时优先修该项并再次提交推送，不开始下一项。远端瞬态故障允许有界重试，无法解决时明确交接已推 SHA、失败证据和剩余项。
6. 在提交台账记录本项 SHA、远端、CI 链接/结果与测试证据。台账信息可放入下一项文档；最终项用交付答复及本地台账记录，避免仅为写自身 SHA 无限追加提交。

## 第 1 项：A5 请求边界的工具调用与结果配对

已核实：真实 Agent 在工具开始后取消，三个客户端下一次 SDK 请求均保留工具调用但无结果。未验证真实远端 HTTP 400；不要把远端错误码作为已有证据。

方案：新增小型、纯函数式配对模块，在 Anthropic、Responses、Chat Completions 三个请求构造入口，对合并 transient reminders 后的消息副本统一规范化；不原地修改 ConversationManager.history、会话文件或审计。完整的正常历史保持内容及顺序稳定，保留 thinking/signature、缓存稳定性和已有空消息处理。

配对必须按工具 ID 和相邻合法消息边界处理，不能只平衡总数。未完成工具补明确错误结果，说明可能已经产生副作用、需复核；已有真实结果优先，不能在提前补位后丢弃稍晚的真实结果。孤儿结果、重复 ID、连续用户消息、空壳消息和中断后新增用户消息分别定义规则并用测试固定。拒绝授权沿用显式 deny 事实，不猜测中断等于未执行；不能通过伪造成功结果让请求通过。

落点：新增 `mewcode/conversation_pairing.py` 或同等低依赖模块；`client.py`、必要的 serialization/Agent 拒绝结果文案及专项测试。已有 fork、压缩、恢复的修补先盘点，避免互相冲突。

验收：

- 真实 Agent 取消后继续提问，三个真实客户端的 SDK 捕获 payload 均满足对应协议的调用/结果 ID、顺序及角色约束；请求副本补位、原历史不变。
- 正常多工具结果、跨轮次、部分完成、迟到结果、孤儿结果、重复 ID、thinking 与 transient reminders 均有有意义的用例。
- 权限拒绝不能被重新解释为执行成功，正常 serialization/context/session/fork 行为不回退；不需要真实远端模型调用才能交付。

建议提交：`fix: repair interrupted tool pairing at request boundaries (A5)`。

## 第 2 项：A3 idle 路由与队友监听生命周期

已核实：idle 写 `lead`，实际消费使用 lead_agent_id；60 次一秒轮询后协程结束但状态仍 listening；SendMessage 对无人监听的成员仍回报成功。完成通知另有独立路径，不能一并推断失效。

方案：统一解析团队实际 lead ID，初次与后续 idle 使用同一路由。团队后台成员保持监听直到显式 shutdown、TaskStop、取消、退出或团队删除；普通非团队后台任务继续一次性完成。引入/复用一个可测试的 worker 生命周期组件，让 A1 的独立 worker 使用同一消息/终止契约，不为每个后端复制不同轮询语义。

不能只将 range(60) 改成无限循环：`poll_completed()` 目前会移除任务表条目，需将“消费一份完成通知”与“删除仍可派工的监听成员”分离，完成通知使用稳定快照/队列记录，保留后续可取消的运行句柄。审查 `has_pending_work()` 与 headless 退出门控，避免新常驻监听导致主程序永远不退出；本轮运行完成后应按既有任务/团队语义收尾所有拥有的监听者，而不是复活主模型请求或依赖无消息 60 秒退出。

处理 text/shutdown_request/shutdown_response、request_id 对应、取消竞态、失败与退出清理。SendMessage 必须对已知离线/已停目标返回准确错误或明确持久投递语义，不能把“写入磁盘”说成“worker 已接收”；本计划默认对已知无消费者的成员报错。运行、监听、停止/取消、失败等状态与实际任务一致；状态可终止不等于进程已回收，后台句柄需 await。

落点：agents/task_manager.py、teams/manager.py、必要的 worker runtime、send_message.py、Agent/TUI/headless 的通知与收尾接线，以及 teams/任务专项。

验收：

- 真实 TeamManager/Mailbox/TaskManager 路径能从 lead drain 收到初次与后续 idle；完成通知仍及时且仅消费一次。
- 受控时钟跨过原 60 轮后仍能再次派工；另运行一次真实超过 60 秒的低成本离线集成，证明不是只改测试时钟。
- 消费完成通知后仍能派工/取消；shutdown、取消、团队删除、失败与应用/headless 退出无孤儿监听和假 listening 状态，不增加模型请求。
- 普通后台任务仍能自动收敛，headless 门控与现有两条通知通道不回退。

建议提交：`fix: align teammate notifications and listener lifecycle (A3)`。

## 第 3 项：A1 pane 队友启动契约

已核实：两个后端复用的命令把 -p 当布尔参数，且带 CLI 不支持的 flags；三个 team 环境变量只有写入没有读取。问题限 pane 后端，不是整个产品不可用。

方案：把 worker 启动 argv 构造集中在一个模块，配套明确的 worker CLI/入口。推荐 `sys.executable -m mewcode --teammate ...`，由单一解析器处理 team、agent ID/成员、工作目录、prompt、角色和 model；不再依赖无人读取的环境变量或末尾裸位置 prompt。worker 从团队配置校验身份与工作目录，复用 A3 的监听/关闭契约、原有权限、工具过滤与 provider 选择，不用直接照搬上游主入口。

父/子 provider 与角色覆盖必须保持一致，明确配置来源；启动 argv、仓库文件和握手/日志不写密钥，不在测试中读取用户配置。工作目录、解释器、prompt、模型与名称含空格、中文、单引号或 shell 元字符时仍是字面参数。tmux/iTerm2 的不同 shell 嵌套独立验证，避免拼接后重新解释参数。

增加有界启动握手/失败反馈：pane 创建/send-keys 成功不算 worker 已就绪；CLI 解析、配置加载或 worker 初始化失败须准确回报，回收本次创建的成员/名称注册、pane 和可安全删除的空 worktree，不遗留假活跃成员、不删除已有修改。仅对本项拥有资源收尾；不默默降级到宿主/其他后端。

落点：新增 teams/spawn_cli.py 与必要 worker 入口；__main__.py、spawn_tmux.py、spawn_iterm2.py、agent_tool.py、团队启动握手/状态。保持 TUI 与普通 `-p` 兼容，Windows in-process 路径不受影响。

验收：

- 用真实生成器与真实解析器往返所有参数，覆盖引用字符、中文路径、空 prompt/缺失字段、未知成员与路径不匹配。
- 离线 worker 真实子进程启动/握手、消费一条消息、shutdown/回收；错误配置/CLI 立即失败且父调用返回错误。
- 具备 tmux 的 Linux 环境进行 fake LLM/替身 worker 真 pane 冒烟，运行存活、握手、派工与退出均有证据。可用临时隔离环境补齐 tmux 测试依赖；无环境时如实列边界，不把 mock pane 计为真机验证。iTerm2 无 macOS 时标为未执行，至少覆盖其参数和调用契约。
- CLI `--help`、普通 headless 与 in-process 团队回归通过；未新增真实模型调用。

建议提交：`fix: unify pane teammate startup and readiness checks (A1)`。

## 第 4 项：A2 实现 TaskStop 并接入实际注册链

已核实：仅在配置与环境双开关启用的协调模式中，提示承诺 TaskStop，但实际未实现。三次未知工具终止是脚本模型受控场景，不保证真实模型必然重试。

方案：新增本仓 Tool 泛型格式的 TaskStop，参数接受团队内成员名/ID，绑定实际 TeamManager/任务运行句柄，不只接一个未被调用的 InProcessTeammateHandle。当前真实队友由 TaskManager.launch 启动；先以 A3 的状态/取消 API 为准，再覆盖 A1 的 pane worker。工具在 TUI/headless 的真实协调模式激活链注册，并留在 coordinator allowlist；普通模式和子代理不能越权控制其他团队。

先发对应 shutdown/cancel 并有界等待，再按拥有的后端句柄终止进程/pane；停止工具回报已停止前须确认收尾。TaskStop 返回是 runtime 状态，不等于删除团队、回滚改动或清理全部 worktree。重复停止幂等；未知成员报错并列出可操作成员，不伪造成功。

决定“停止后再派工”的明确语义：本计划将 TaskStop 作为当前 worker 的终止操作，SendMessage 对已终止 worker 报错，由 Agent 显式重新创建/派工，不靠无消费者的邮箱复活。同步修正 coordinator 文案中“Stopped workers can be continued with SendMessage”的过度承诺；正常 idle 仍可继续派工。若在实施中选择保留可恢复的暂停，必须真实实现并测试暂停/恢复状态机，不能只改文案而暗中不兑现。

落点：tools/task_stop.py、TeamManager/TaskManager 运行句柄、TUI/headless/TeamCreate 激活注册、tool_filter、coordinator 提示及测试。预留但未暴露的其他工具名不在本项顺手清理。

验收：

- 实际模式切换后 schema 包含 TaskStop，普通/子代理模式不包含；调用不再 unknown tool。
- 对真实 TaskManager 启动的运行中与监听中成员均可停止并 await；取消/停止通知、状态与重试停止一致。
- pane 先协作退出、超时后只关闭该成员的 pane；不存在的成员、已停止成员、跨团队 ID、竞争完成等场景行为明确。
- 停止后的 SendMessage 与提示承诺一致，未删除用户修改或虚构恢复。

建议提交：`fix: implement coordinator TaskStop across teammate backends (A2)`。

## 第 5 项：A4 自动写入与类型化召回衔接

已核实：自动记忆写入 memories.md，召回扫描 memory/*.md；没有候选时选择器调用 0 次，预置类型化文件时正常。不是所有长期记忆都不可用，不能以当前存在 token 浪费为理由直接关掉功能。

采用兼容的确定性衔接，避免本轮重写全部记忆架构：保留既有扁平写入、load 和用户/项目分类作为兼容事实来源，将自动管理的类型化文件及 MEMORY.md 指针作为可重建派生视图。使用独立小模块，从已合并的真实扁平条目按分类生成合法 frontmatter（user/feedback/project/reference）；稳定 ID/内容摘要确保重复提取和重复回填幂等。只维护有明确归属标记/manifest 的自动条目，不覆盖用户手写或第三方类型化文件，不把同名文件误认成自动产物。

存量扁平记忆在明确的记忆初始化/写入衔接点做一次幂等回填，不在每条消息中无界重扫重写；不改变 load 既有输出、不删除/搬走旧文件，也不让派生视图反向覆盖事实来源。持久写入需原子、并发安全、路径受控，索引可从条目重建，失败时保留扁平事实并给出诚实诊断/重试机会。继承已有条目追加/纠正语义，不因新提取未重述旧记忆而删掉旧条目。

调用成本边界：不用 LLM 做迁移、拆分或再分类，不添加额外提取轮次。原有召回选择器在候选出现后可能实际执行，指南必须说明这一变化；避免把已完整注入同一请求的派生条目再次召回或为其额外调用选择器，保留手写类型化文件既有召回行为。候选为空时仍不创建 selector 客户端，不声称节省未经计量的 token。

落点：memory/auto_memory.py、独立派生视图模块、recall 候选/已注入过滤、app 接线与 memory/recall 测试。不得调用真实用户记忆路径验证，不引入后台记忆整理、自动清空或全量格式迁移。

验收：

- 调用真实 MemoryManager 写入/初始化后，扫描能看到正确 scope/type 的自动条目；强制选择的离线 selector 可选中并渲染正文。
- 旧 load 输出保持兼容；存量回填幂等，重复提取不增殖，手写类型化文件和索引已有用户条目保留。
- 用户/项目两 scope、纠正条目、Unicode、路径穿越名称、同名文件冲突、写入失败与并发提取有真实临时文件测试；原始扁平事实不丢失。
- 已注入条目不额外触发重复召回；空候选 0 次 selector；手写类型化文件正对照仍工作，不发生真实模型调用。

建议提交：`fix: connect automatic memory writes to typed recall (A4)`。

## 最终交付

五项分别有完成状态、提交 SHA、远端 SHA、对应 CI 链接和本轮验收结果；统一列出尚未执行的真实平台/端点验证。保持黑色 TUI 已确认外观，终端待办 1 的实窗人工项继续按原记录保留，不混入本轮五项修复完成声明。若发现新缺陷，仅修复阻碍本项正确性的必要部分并说明，不顺手扩展 B/C/V 工作。
