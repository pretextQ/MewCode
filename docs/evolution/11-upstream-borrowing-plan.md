# 参考库能力借鉴：开发与逐项交付计划

日期：2026-11-02。起点：`60966774dbf8e1f1cee2e02d8c072c4c767af42d`。
依据：[当前对照报告](../review/review-2026-11-02-current-borrowing.md)及其中源码证据。执行记录：[逐项交付台账](../stage-2026-11-02-borrowing.md)。

## 授权与范围

2026-11-03 恢复：用户明确恢复 U5–U7，按下述顺序连续实施；U1–U4 不重做。上一项最终 SHA 的五作业 CI 成功后进入下一项，执行结果以台账和实时 Git/Actions 为准。以下 2026-11-02 暂停说明保留历史，不再作为当前停止指令。

2026-11-02 交付更新：U1–U4 已完成实现、验证与独立提交推送，各项对应五作业 CI 成功；U4 最终产品提交 `f2ac5e192fe72dc9dcccda4d451f618743b2f9d5` / [CI 41456722525](https://github.com/pretextQ/MewCode/actions/runs/41456722525)。用户要求 U4 后暂停（额度不足），现已暂停；U5–U7 未实施，保留以下设计，等待用户明确恢复。本次只同步文档，不恢复开发。此更新优先于下文原连续授权，完整交付与未解决事项见[台账](../stage-2026-11-02-borrowing.md)。

用户明确要求：“接下来要为这些借鉴点开始实施了，先帮我为这些借鉴点规划好开发文档，然后开启新会话按照开发文档逐步实施，每完成一个就提交推送。”

本计划授权新会话连续完成 U1–U7 的设计细化、实现、必要验证、文档、独立提交、正常推送以及对应 CI 核验，无需重新逐项询问。按当前 `master` / `origin/master` 执行。规划文件由本会话先单独提交推送，产品实现交给新会话。历史 A1–A5 不重做，旧报告和观察结果保留当时事实。

本轮包括：写保护、指令加载、崩溃诊断、拒绝反馈、编辑 diff、MCP eager/dispatch、只读 Shell 并发及退出码展示。报告“后续再考虑”的自定义命令、技能安装器、后台记忆整理、浏览器 UI 不在本轮实现范围。MCP native/tool_reference 留待独立协议验证，本轮不引入未经验证的 beta header。

用户只在 Windows/Linux 使用；不为 macOS/iTerm2 新增验收任务，不删除已有后端。保持已确认的黑色 TUI。没有真实模型调用预算，使用脚本模型、SDK 替身、临时文件及本机/隔离 Linux 测试；不启动用户真实 TUI 会话。

## 顺序与交付单元

| ID | 开发单元 | 前置 | 完成条件 |
| --- | --- | --- | --- |
| U1 | 受保护配置及技能定义禁写 | 无 | 各权限模式及实际文件工具路径验证，专用管理操作兼容 |
| U2 | AGENTS.md、目录链及受控 include | U1 交付 | 加载顺序、来源、范围和所有入口一致 |
| U3 | 崩溃诊断与日志保留 | U2 交付 | 跨启动记录、异常入口、日志路径与失败降级验证 |
| U4 | 授权拒绝附带反馈 | U3 交付 | 交互到下一请求 payload 的完整闭环 |
| U5 | 编辑成功后的有界 diff | U4 交付 | 准确增删、截断和现有文件语义兼容 |
| U6 | MCP 按规模加载及兼容分发 | U5 交付 | eager/dispatch、真实目标门禁、集合稳定性及回退 |
| U7 | 按参数判断只读 Shell 并发 | U6 交付 | 保序、独占、权限/hooks 和退出码契约验证 |

上一项提交的五个 CI 作业成功后再进入下一项产品修改。每项完成即提交推送，不把七项攒成一个提交。必要修正可追加同 ID 提交，台账记最终有效 SHA。各项 ID 与旧报告 B1–B10 无一一编号对应，以本计划为执行口径。

## 所有单元共用的开发流程

1. 重新核对 `git status`、暂存区、HEAD、远端与适用指令。当前 `docs/README.md` 有一条旧本地审查入口的用户改动，保留且不夹带；不能 reset、stash 或整文件暂存覆盖它。发现并行改动先区分所有权，避免互相覆盖。
2. 阅读当前源码，按本项验收补有意义的失败证据；已有同义测试直接复用，不堆叠镜像断言。新能力用临时项目/home、假 LLM 或本地 MCP 替身验证，不读取真实配置和记忆。保留失败原因与修正记录。
3. 实现共享逻辑并接入实际入口，不能只写独立 helper 或 mock 测试。遵守 `scripts/source_budgets.json` 的现有上限和架构守卫，必要时抽小模块，不提高 ceiling。尤其 Agent 当前接近 1575 行上限。
4. 每项运行必要专项、全仓 Ruff、全包 Windows/Linux 目标 mypy、工程守卫、锁文件与 diff 检查。U1/U2/U4/U6/U7 涉及共享执行/权限/请求，应执行全量 pytest；U3/U5 至少完成相应端到端及受影响回归，若改了共享行为也跑全量。Docker/真进程/MCP 集成仅在跨过相关边界时增加。固定通过后不无理由重复全套。
5. 使用现有环境及锁文件：`uv run --locked ruff check .`、`uv run --locked mypy --platform win32 mewcode`、`uv run --locked mypy --platform linux mewcode`、`uv run --locked python scripts/engineering_guards.py`、`uv lock --check`。全量入口 `uv run --locked pytest -q --maxfail=10 --timeout=120`；首选按路径选专项，不把未知平台替身当真实平台通过。
6. 更新对应公开指南、台账和 `docs/next-session-prompt.md`；只记录本轮真实运行的命令、结果、平台和未覆盖边界。旧历史数字不覆盖，新功能不能仅凭默认关闭就声称已节省费用/提高质量。
7. 逐文件/逐块暂存本项文件与必要文档，检查 `git diff --cached --check` 和完整 staged diff；只提交本项范围，不提交配置、密钥、原始模型响应或整片 evidence 目录。英文提交信息含 U 编号。
8. 正常推送 `origin/master`，不强推；若远端有新提交，保留用户改动并正常集成，重新验证受影响部分。核对远端 SHA 与本地 SHA，查询该 SHA 对应 push CI 的 Windows/Linux × Python 3.11/3.13 四作业及 security 作业，不用其他 SHA 的绿灯替代。GitHub 工具不能列 push 运行时可用已登录的 gh/API，不将“查询不到”当通过。
9. CI 失败先修当前项并重新提交推送；外部瞬态失败可以有界重试。仍无法解决时明确记录失败及剩余项，不能带红灯推进。最后一项的 SHA/CI 可在交付答复及本地台账记载，避免为写自身 SHA 无限追加提交。

## U1 受保护配置及技能定义禁写

### 设计决策

引入写入专用保护判定，覆盖项目 `.mewcode/config.yaml`、`permissions.yaml`、`permissions.local.yaml`、`skills/`，以及全局 `~/.mewcode` 下同类目标。仅阻止普通 Agent 文件写入，不能因保护配置而禁止授权范围内读取；不把它描述成任意 Shell 的 OS 沙箱。

按真实路径及目录边界判定，沿用当前 project-root/绑定 work_dir 解析约定；考虑不存在目标、父级链接、Windows junction/大小写/分隔符与路径穿越，不用裸字符串前缀。保护检查在文件写入的 PLAN 例外、显式 allow 和权限模式兜底之前执行，BYPASS 也不能豁免；已有危险检测、规则 deny 优先和越界拒绝语义保留。

`RuleEngine.append_local_rule` 的“始终允许”由应用专用管理路径继续工作，不伪装普通 WriteFile。先审计现有技能注册命令的写路径，仅保留用户显式命令的已有管理能力，不扩大为模型可任意绕过的开关。自动记忆、会话、计划目录继续按现有路径工作。

落点：`permissions/sandbox.py`、`permissions/checker.py`，必要的独立 `permissions/protected_paths.py`；按实际需要适配文件工具目标解析和专用管理接口。更新 `guides/permission-semantics.md`。

### 验收

- 实际 Agent 的 WriteFile/EditFile 在 DEFAULT、ACCEPT_EDITS、DONT_ASK、BYPASS、PLAN 各模式都拒绝保护目标，确认文件不变；普通源码、允许读取、正常 plan 与记忆写入有正对照。
- 相对路径、绑定 worktree、大小写、近似名称、链接/不存在文件分别测试；Windows/Linux 各用真实可用的文件系统特性，平台缺失如实 skip。
- “始终允许”真实持久化、既有 deny 优先和串行/并行/非交互入口不回退，不新增 Shell 安全承诺。

提交：`feat: protect agent configuration and skill definitions (U1)`。

## U2 项目指令加载

### 设计决策

采用明确顺序：全局 `.mewcode/MEWCODE.md`、`.mewcode/AGENTS.md`；随后从最近 Git 根到工作目录，逐级读取 `MEWCODE.md`、`AGENTS.md`、`.mewcode/MEWCODE.md`；最后工作目录 `MEWCODE.local.md`。无 Git 根时只扫工作目录，不向盘根收集外部指令。`.git` 可以是 worktree 文件。后面的文本优先是约定，不宣称可强制模型遵循。

各段带来源标签，真实路径去重。保留 `@include path`，增加 seen 循环检测、代码围栏排除、受控读取错误处理和总量预算。首期无需额外引入 `@./path` 语法；保留兼容格式更重要。项目指令 include 限仓库根内；全局指令 include 限全局 `.mewcode` 内，禁止任意绝对路径或跨 scope 读取。限制深度、单文件/总字节和目录层级，预算耗尽给出简洁诊断，不无界膨胀上下文。默认预算在实现时选保守常量并在指南说明。

复用共享 loader 接入 TUI、headless、service、独立 teammate 的实际构造链，路径基准是运行 work_dir。fork 的上下文继承遵循现有契约，不能每轮重复注入。此期只按运行目录加载，不实现“编辑任何文件时动态追加其子目录规则”。

落点：`memory/instructions.py`，必要的轻量 include 模块和实际调用入口。新增 `guides/project-instructions.md`，示例保持无凭据。

### 验收

- 临时全局/root/中间目录/叶子/本地覆盖文件的顺序、来源、去重和无 Git/worktree `.git` 文件验证。
- 中文路径、包含链、环、fenced code、链接越界、缺失/解码失败、预算耗尽均有边界验证，错误文件不能使整个 Agent 启动崩溃。
- 用脚本 LLM 捕获 TUI/headless/service/worker 的实际请求或共享构造参数，证明 loader 结果送达且无重复；避免仅测试独立函数。
- 保留原 MEWCODE.md 加载及注入幂等测试，变更加载顺序作为明确兼容行为记录。

提交：`feat: load scoped project instructions and AGENTS.md (U2)`。

## U3 崩溃诊断与日志保留

### 设计决策

新增小型诊断模块，记录启动、正常退出、异常上下文及堆栈、进程/会话标识，安装 faulthandler。接到 CLI/headless、service 及 Textual 的真实异常入口，避免重复安装、重复上报；不吞掉原异常或改变退出码。无法捕获的硬终止只能如实显示日志缺少退出标记，不能猜成已定位崩溃。

保留 `MEWCODE_LOG_FILE`，默认诊断文件与有效日志位置协调；容器重定向后诊断也不能落进修复 worktree。普通 debug.log 从覆盖改为追加/轮转；faulthandler 持有的描述符不随轮转失效，必要时为其保留独立有界生命周期文件并明确清理策略。多进程以 PID 区分，文件打不开时降级，日志失败不能导致应用二次崩溃。

不记录完整请求、会话正文或配置；对异常文本里的常见凭据作必要清理，说明 faulthandler 仅提供堆栈线索，不声称能捕获所有异常退出。

落点：新诊断模块、`__main__.py`、app 异常入口及 service 启动接线，新增 `guides/diagnostics.md`。

### 验收

- 临时日志目录下运行真实子进程，验证两次启动保留历史、异常堆栈、正常 exit、非零退出码与幂等安装；不在主测试进程触发致命故障。
- 真实子进程 dump_traceback 或等效安全故障探针证明 faulthandler 接线，不能只 mock install。
- 重定向/不可写目录、轮转、并发 PID、敏感样例清理和应用原有关闭行为验证；service 日志不污染工作产物。

提交：`feat: retain process diagnostics and crash traces (U3)`。

## U4 拒绝授权时附带反馈

### 设计决策

增加结构化回复 `PermissionReply(response, feedback="")`，在迁移期接受旧 PermissionResponse 值，由共享规范化函数处理，避免旧测试/回调悄然失效。文本只用于 DENY，其他选择丢弃草稿；无反馈仍返回原有拒绝语义。

内联授权框选择 No 后可输入/粘贴短反馈，设置长度上限，转义 markup/终端控制内容，明确 Escape 是无反馈拒绝，Ctrl+C 走已有取消收尾。反馈只进入对应真实拒绝 tool_result，下一次原有模型请求即可看到，不额外制造用户轮次/模型请求，也不改变请求配对模块对中断未知副作用的描述。

实际 TUI 回复 handler、串行/并行 ask 升级、非交互拒绝及 worker 调用适配统一协议。headless 没有 UI 时不发明反馈；不把拒绝当成功或自动授权替代操作。

落点：独立权限回复/结果构造模块、Agent 类型及执行路径、`permission_dialog.py`、`app.py`。更新权限与 TUI 指南。

### 验收

- Textual 离线交互测试：选择/输入/粘贴、转义、长度上限、切换选择丢草稿、Escape、Ctrl+C，输入区恢复且 future 只完成一次。
- 用真实 Agent + SDK 捕获证明 DENY 后文件不变，反馈出现在下一请求的目标 tool_result；无反馈/ALLOW_ALWAYS 保持原行为。
- 三客户端请求配对、并行 ask、非交互、取消竞态和既有权限安全回归通过；重试不能把草稿泄露给其他调用。

提交：`feat: pass user feedback with permission denials (U4)`。

## U5 编辑后有界 diff

### 设计决策

编辑成功后返回文件名、准确 additions/removals 和带上下文/行号的预览。使用可靠的分组 diff，而非把最长公共前后缀之间全部算成修改。增删统计来自完整受支持输入，展示最多 200 行/约 12000 字符并标记截断；超大输入采用有界成本策略，无法精确计算时明确“统计省略”，不得编造精确数字。

先完成当前读取状态校验、唯一匹配、编码/混合换行保留、历史跟踪、实际写入及缓存失效，再生成展示。预览失败不能使已经成功的编辑被伪报为未执行；写入失败没有成功 diff。TUI 工具详情复用现有展示，过滤控制字符/markup，流式与恢复历史保持一致。

落点：新增独立 diff helper、`tools/edit_file.py`、按需调整 `tui/tools.py`；不顺便重写文件编辑器或扩展文件写入授权。更新 `guides/tui.md`。

### 验收

- 分散多处修改、纯插入/删除、空文件、末尾换行、Unicode、CRLF/混合换行及超大输出，统计与展示分开验证。
- 真实临时文件编辑后字节符合预期；原读后写门禁、撤销、状态缓存失效、self-verify 观察器不回退。
- 已完成写入但预览异常、写入失败、历史工具块及恶意控制字符展示均有覆盖，不泄露整份大文件到上下文。

提交：`feat: show bounded diffs after successful edits (U5)`。

## U6 MCP 按规模加载与兼容分发

### 设计决策

先用本地 fake MCP/合成 schema 记录 0、小、大工具集的 schema 序列化大小及发现前后 tools 的变化，结果入本项验收。只证明确定性行为，不声称实际端点缓存命中率或账单节省。

在现有依赖上实现 `legacy`（当前默认）、`eager`（全部可见）、`dispatch`（ToolSearch + 单一调用入口）及显式选择的 `auto`。auto 根据会话启动时 schema 估算与上下文预算选择 eager/dispatch，阈值和估算方法文档化，按会话固定；默认仍 legacy，避免未经真实模型验证就改变所有用户行为。服务/headless 默认继续既有直接可见、只读 MCP 契约，可单独测试显式配置路径。

dispatch 中 MCP schema 不随搜索加入 tools；ToolSearch 返回完整目标 schema，统一入口只接受明确的 MCP 注册工具身份。保持现有 `mcp_server_tool` 名称及权限配置，不全局改名。注册映射发现碰撞应明确报错，不能用任意后缀/跨 server 猜测；未知、禁用、被过滤和非 MCP 目标均拒绝。

重点：解析目标之后必须走**真实目标**的注册启停、角色 allowlist、PermissionChecker、hooks、参数校验、监督观察与 MCP 只读策略。不能只给外层 dispatcher 做门禁，再直调隐藏目标绕过限制。保留目标完整 schema；首期拒绝无效参数并给清晰反馈，不照搬含糊的逗号字符串拆数组等自动修正。统一入口不得递归调用自己。

可抽共享执行门禁接口解决依赖方向，不能让 `tools/impl` 反向 import Agent，不能复制一套不一致权限循环。明确外层/内层 hook、审计和工具结果 ID：每个真实目标恰好记录一次执行事实，外层协议结果绑定原 tool_call ID；取消后结果未知仍由 A5 请求副本修复。

加载模式在初始 MCP 连接完成后确定；显式重连/能力变化可以建立新的工具代际并明确通知，不能为“字节稳定”保留已撤销权限。稳定性只保证工具和模式未发生实际变更的会话段。失败不默默扩权，用户可显式切回 legacy。

落点：新 loading strategy、MCP dispatcher、ToolRegistry/ToolSearch、共享执行接口、配置及各入口接线；新增 `guides/mcp-loading.md`。不升级到参考库另一套 `mcp.Client` API，不加入 native beta，不调用外部端点。

### 验收

- 三种客户端的 fake SDK 请求捕获：legacy 原行为、eager 初始可见、dispatch 搜索前后 tools 序列化稳定、schema 可见及统一入口真实完成调用；空集合与 auto 阈值边界有覆盖。
- 用本地真实 stdio MCP 替身验证连接、搜索、调用、失败、超时/取消和回收；不能只测策略函数或 mock dispatcher.execute。
- 配置 allow/deny、disabled、plan/子代理过滤、readOnlyHint、跨 server/同名/碰撞、非 MCP 目标及恶意参数的正反例；特别验证 dispatch 不能调用服务模式剔除的写工具。
- hooks 与监督不漏/不重复，原 tool ID、调用/结果配对及权限 UI 反馈不回退；运行现有 MCP 并发/生命周期/服务集成回归。
- 台账给出合成规模数据与确切稳定性范围，native/真实缓存效果标为未验证，不阻塞本项 eager/dispatch 交付。

提交：`feat: add compatible MCP loading and dispatch modes (U6)`。

## U7 只读 Shell 并发及退出码展示

### 设计决策

新增明确的按参数并发判定接口（例如 `can_run_concurrently(arguments)`），默认兼容现有布尔 `is_concurrency_safe`。所有分批路径统一调用它；不能把布尔改成同名方法后由方法对象真值放行。U6 dispatcher 若承载隐藏目标，首期保持保守串行，不能因外层是“检索相关工具”就猜其安全。

首期只对经过严格词法检查的少数确定查询并发，如 `pwd`、无副作用参数的文件读取及 `git status` 的受控形式。权限自动放行白名单不等于并发安全白名单，不直接复用前缀判断；排除 git 的写参数、别名/外部 diff/filter/pager、输出文件、未知 option、Shell 链式/管道/展开、环境赋值、重定向及无法可靠解析的 Windows/POSIX 语法。宁可串行，不新增权限自动放行或后台执行能力。

保持相邻只读批次并发，写操作/普通命令作为顺序屏障。每个工具照常走本项前的完整权限、hooks、监督；结果按提交顺序绑定 ID，异常/取消等待已拥有的子进程收尾。

Shell 结果明确附带数值退出码。未知/复合命令和测试失败仍 is_error=True；只对可可靠识别的单一 `rg/grep` 无匹配与 `diff` 有差异等定义少数语义例外，其余保守判错。不得将所有非零视为成功；保留输出上限、编码处理、真实进程树取消与超时。

落点：Tool 基类、独立 Shell 判定 helper、Bash、Agent 所有批次入口、MCP 适配；更新权限指南与 Agent 运行指南。

### 验收

- 实际 Agent 批次中两个受控只读 Shell 可以重叠，读→写→读不能越过写屏障；原只读工具并发继续工作。
- 用受控事件和真实低成本子进程记录执行先后，不仅比较墙钟耗时；结果 ID/顺序准确，单个失败不串结果。
- 串行/并行的 deny、ask、pre/post hooks、监督、子代理 work_dir 和取消收尾一致；恶意/未知参数被判定串行，不靠真实执行危险命令验证。
- Windows/Linux 分别覆盖实际 shell；退出 0/1/7、grep/rg 无匹配、diff 有差异、pytest 非零、复合命令和超时的返回语义明确。
- 台账记录小型离线并行实验与边界，不宣称未经真实任务测量的提速百分比。

提交：`feat: schedule validated read-only shell commands concurrently (U7)`。

## 最终验收与交接

七项都应有真实完成状态、对应最终 SHA、远端 SHA、CI 链接/五作业结果、必要测试和公开行为文档。最终运行一次覆盖全部累积变更的全量与工程检查（若 U7 最终已做，无需重复），检查构建/打包能包含新增模块；保留已知跳过和平台限制。

不把离线协议接线等同真实模型遵循，不把已启动任务等同任务已完成。2026-11-03 已明确恢复并实施 U5–U7，U1–U7 的实际验收数字、最终产品 SHA 与 CI 以台账和实时 Git/Actions 为准，不用历史绿灯替代本项。遇到本项所必需的实现选择可自行收敛并记理由，不因原报告提到“待决策”就反复请求批准。
