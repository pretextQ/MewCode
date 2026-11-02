# Agent 请求与队友运行契约

项目适用与验收范围为 Windows/Linux。用户明确不在 macOS 使用；以下 iTerm2 内容仅记录已有后端契约，macOS/iTerm2 实窗验证不作为本项目待办或交付缺口。

## 中断后的请求配对（A5）

三个 provider 客户端在合并 transient reminders 后，对发送副本规范化工具调用与结果，不修改真实历史、会话文件或审计。正常历史保留正文、thinking/signature 和缓存标记策略。

结果按原 ID 绑定到最近的调用批次，同一批次中的重复 ID 按调用先后配对，并仅在请求副本中生成唯一 ID。迟到的真实结果优先，移动到调用之后；部分缺失的结果补错误说明：执行状态未知，可能已有副作用，重试前应检查实际状态。明确的权限拒绝保留原事实。孤儿或重复结果转为带来源说明的普通文本，保留证据但不冒充已配对执行。

回归入口：`tests/test_request_pairing.py` 使用真实 Agent 取消和三个客户端的 SDK 捕获边界，检查角色、相邻顺序、ID、迟到/重复结果与历史不变。此为离线请求构造验证，未调用远端模型、未验证远端 HTTP 错误码，也未替代 TUI 实窗人工验收。

## 队友监听与退出（A3）

团队 worker 每轮完成后向实际 `lead_agent_id` 发送 idle，保持监听直到 shutdown、取消、失败、团队删除或所属应用退出。普通非团队后台任务仍一次完成。TaskManager 的完成通知是每轮快照，只消费一次；消费通知不会删除仍可派工、取消的监听句柄。

`SendMessage` 只向本团队 lead 或有消费者的成员投递；已知离线成员返回错误。广播中存在离线目标时整批报错，不将持久写入冒充接收成功。shutdown_request 的回应保留 request_id，shutdown_response 不触发模型执行。

headless 在仍有执行轮次、已接受但未消费的派工或未消费通知时继续等待；只剩 idle 监听者时结束本轮，并取消、等待所属后台句柄。TUI 退出和 TeamDelete 也先 await 收尾再删除邮箱。同步直接删除仍有监听者的团队会报错，调用者应先 await `shutdown_team`。

验证入口：`tests/test_worker_lifecycle.py`；真实超过 60 秒的低成本离线用例可用 `MEWCODE_LONG_LISTENER_TEST=1` 开启。该测试使用临时 home/cwd、真实邮箱与任务管理器、脚本 worker，无模型调用。普通 CI 使用加速时钟跨过原 60 次轮询，并覆盖真实 headless 创建团队/派工/通知/退出链路。

## 独立 pane worker（A1）

pane 后端使用单一 `build_worker_argv` 和 `mewcode --teammate` 解析器，普通 `-p PROMPT` 与 in-process 入口保持兼容。参数使用字面 argv，再按 POSIX shell 规则引用；tmux 直接以命令创建 pane，不依赖向尚未就绪的 shell 注入按键。iTerm2 使用系统 osascript 和 session unique ID，命令作为 AppleScript 的 argv 传入。调用依据：[tmux 手册](https://man.openbsd.org/tmux)、[iTerm2 脚本文档](https://iterm2.com/documentation-scripting.html)。

父进程通过一次性、随机令牌保护的本机引导通道发送有效 provider、角色指令、身份、权限规则路径、允许工具和可选 fork 历史。密钥只在内存通道传递，不写 argv、团队配置或就绪文件。worker 校验成员、角色、model 与 worktree，建立受父白名单限制的基础/协作工具集，不自动启动额外 MCP 服务。监听复用 A3 runtime，后续消息保留 worker 会话。就绪只说明初始化成功，不表示初始任务已完成。

父进程最多等待 10 秒就绪，初始化退出或超时返回错误；不会自动改换后端。失败时确认回收本次 pane，撤销本次成员、名字与 trace，仅自动删除本次新建且无改动的 worktree。失败前已有的同名注册、已有/有改动的 worktree 保留；无法回收时明确报错并保留可追踪记录。tmux pane 的进程 ID 用于避免误关被复用的 pane ID。团队删除也保留有改动、有新提交或基线未知的 worktree。

验证入口：`tests/test_pane_worker.py` 覆盖真实 CLI、离线 SDK/子进程、错误初始化与失败资源清理。Linux 可用 `MEWCODE_TMUX_TEST=1` 在隔离环境运行真 tmux；本轮 Docker 的 network=none，HTTP 模型替身只在容器本机响应。iTerm2 仅测参数/脚本调用契约，未执行 macOS 实窗；PTY/pane 验证不替代原终端待办 1 的人工验收。

## TaskStop（A2）

TeamCreate 在配置 `enable_coordinator_mode=true` 且环境 `MEWCODE_COORDINATOR_MODE=1` 同时满足时，实际注册 TaskStop 与团队 lead 的 SendMessage。TaskStop 不加入普通模式的完整注册表，也从普通子代理和两类队友工具集中剔除。它只控制当前 lead 拥有的团队，参数 `agent_id` 接受成员名或成员 ID，可选 `team_name` 消除重名；未知、跨团队或歧义目标返回可操作成员列表。

TaskStop 是终止操作：运行中和 idle 成员都可停止，重复调用幂等。TaskManager 的真实 launch 句柄会取消并等待收尾；pane 先发送 shutdown，运行时独立检查控制消息、取消并等待当前轮，超时才关闭拥有的 pane，再确认它已退出。无法确认收尾返回错误，不提前宣称 stopped。停止通知与任务状态使用 stopped；普通取消仍是 cancelled。

停止保留团队、worktree 和编辑；它不回滚副作用。停止后的 SendMessage 返回离线错误，需要显式创建新 worker。正常 idle 成员仍可继续派工。验证入口：`tests/test_task_stop.py` 和 `tests/test_pane_worker.py` 的运行中真 tmux TaskStop 集成；所有模型使用离线脚本或本机 HTTP 替身。

## 自动记忆与类型化召回（A4）

用户/项目 `.mewcode/memories.md` 继续作为兼容来源，`load()` 输出保持原语义。初始化和提取写入后，将分类条目确定性派生为 `memory/auto-<type>-<摘要>.md`，并维护 `MEMORY.md` 的自有指针区域。用户偏好、纠正反馈、项目知识、参考资料分别映射为 user、feedback、project、reference；不搬走原文件，不调用模型迁移、拆分或再分类，不增加常规提取轮次。

派生文件使用自有标记、清单和内容校验共同确认归属；同名手写文件、被用户编辑的派生文件、手写索引和已有指针均保留。文件名由内容摘要生成，记忆正文中的路径不会成为写入路径。线程锁与有界 OS 文件锁覆盖读取、合并和写入，文件原子替换；中断留下的所有权意图可重建，重复回填和重复提取不增殖。自动派生目录/清单/索引的符号链接不会被穿透写入。

新的条目文件先完整写入临时文件，再原子发布，目标若被并发创建则拒绝覆盖并保留来源等待重建；既有条目仅在内容一致时复用。索引写入前复核快照，有并发编辑则保留编辑并重试派生。

派生失败保留扁平来源并记录诊断，`refresh_derived()` 或重新初始化可重试；写入来源失败不推进提取游标。原有显式 clear 同步处理仍属自动管理的派生项，保留手写文件；待重试删除的自有项不会进入召回。

TUI 召回按当前请求实际已注入或首次即将完整注入的扁平条目过滤自动派生项。过滤发生在候选上限裁剪前，已注入自动条目不会挤掉较旧的手写文件。没有剩余候选时不创建 selector 客户端；手写类型化文件仍可正常召回。尚未注入的新自动条目出现后，原有 selector 可能实际执行，因此不能声称本改动减少 token 或测得模型质量提升。side-query 客户端在结束/取消时关闭。

验证入口：`tests/test_auto_memory_view.py`，使用临时 home/cwd、真实并发文件与脚本 selector，覆盖存量回填、原始 load、正文召回、手写冲突、路径/符号链接、失败重试、线程/进程并发与 selector 调用次数。没有读取真实用户记忆做内容验证，没有真实模型调用。

## 参考库借鉴运行契约（U1–U4）

- **U1 写保护**：普通 Agent 文件写入不能改项目/全局 `.mewcode` 的三种配置及技能定义，先于 PLAN 写例外、显式 allow 和权限模式放行；授权读取与“始终允许”的专用持久化路径保留。这不是任意 Shell 的 OS 沙箱，详见[权限语义](permission-semantics.md)。
- **U2 项目指令**：共享 loader 按全局、仓库根到运行目录、本地覆盖顺序加载并标来源，支持 AGENTS.md、去重和有界 include；独立代理按自己的运行目录加载，fork 继承上下文。只按运行目录加载，不随任意编辑目标动态追踪子目录规则，详见[项目指令](project-instructions.md)。
- **U3 诊断**：CLI/headless/service/worker 和 Textual 异常入口记录进程线索，普通日志追加轮转、faulthandler 使用独立稳定句柄；日志故障不改变应用原退出码。缺少退出标记不能直接证明崩溃原因，详见[进程诊断](diagnostics.md)。
- **U4 拒绝反馈**：共享规范化兼容旧 PermissionResponse 与新 PermissionReply；只保留 DENY 的反馈，其他选择丢草稿。反馈只进入该次拒绝的错误 tool_result，随原有下一请求送达，不新增用户轮次或模型请求。原请求身份绑定防止迟到回复回答下一次授权；无人值守不虚构反馈，详见[权限语义](permission-semantics.md)与[TUI 指南](tui.md)。

四项均已交付；完整提交、对应 CI、历史失败与离线验证限制见[台账](../docs/stage-2026-11-02-borrowing.md)。U5–U7 尚未实施，按用户要求暂停；没有真实模型效果或缓存收益结论。
