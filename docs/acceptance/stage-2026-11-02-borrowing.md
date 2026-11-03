# 参考库能力借鉴逐项交付台账

日期：2026-11-02。执行规范与完整验收：[11 号开发计划](../evolution/11-upstream-borrowing-plan.md)。原始能力比较：[对照报告](../review/review-2026-11-02-current-borrowing.md)。

2026-11-03：用户明确恢复 U5–U7，连续实施与逐项 CI 门禁生效；下方暂停文字为历史。预检本地/实时 origin/master 同为 `7a08ec06db5d8689bf66cc4243666598659eb575`，暂存区为空，仅保留 docs/README.md 原有用户入口；未发现适用 AGENTS.md。该文档提交 push CI [41459756578](https://github.com/pretextQ/MewCode/actions/runs/41459756578) 已通过公开 Actions API 核实 SHA/event 与五作业全部 success（连接器网络失败，gh 不在 PATH，未将查询失败当成功）。配置未恢复等历史事实继续保留。

用户已授权按 U1 → U2 → U3 → U4 → U5 → U6 → U7 逐项交付。U4 后曾因额度暂停，2026-11-03 明确恢复并继续完成 U5–U7；上一项五作业 CI 成功后才开始下一项，当前结果与边界见表首和下方最新记录，历史暂停说明保留追溯。

| ID | 项目 | 状态 | 最终产品/验收提交（均已推送核对） | 对应 CI | 本轮验收 |
| --- | --- | --- | --- | --- | --- |
| U1 | 受保护配置/技能禁写 | 已推送，五作业成功 | `c81aeb60fc0d4557c7318b1addc96f50cdd553a8` | [41424253094](https://github.com/pretextQ/MewCode/actions/runs/41424253094) | 全量 1823/24；保护专项 60/1 |
| U2 | AGENTS.md/目录链/include | 已推送，五作业成功 | `981619f139a47f619826be6ea88dcda1893ffafd` | [41427433417](https://github.com/pretextQ/MewCode/actions/runs/41427433417) | 全量 1871/25；指令专项 11/1 |
| U3 | 崩溃日志/保留诊断 | 已推送，五作业成功 | `a51c67aaa98eeef75be47402d178a697495e67e7` | [41430601257](https://github.com/pretextQ/MewCode/actions/runs/41430601257) | 全量 1879/26；最终诊断专项 11/1 |
| U4 | 拒绝授权附带反馈 | 已推送，五作业成功 | `f2ac5e192fe72dc9dcccda4d451f618743b2f9d5` | [41456722525](https://github.com/pretextQ/MewCode/actions/runs/41456722525) | 全量 1900/26；反馈专项 18；三平台补充专项通过 |
| U5 | 编辑后有界 diff | 已推送，五作业成功 | `d456498e6bd7bda9951e229edce128b5a0fd6681` | [41560096587](https://github.com/pretextQ/MewCode/actions/runs/41560096587) | 全量 1916/26；最终展示专项 59 |
| U6 | MCP eager/dispatch | 已推送，五作业成功（attempt 2） | `61290369531dbb06e1297efd08dca14ea0cb7b1d` | [41565098297](https://github.com/pretextQ/MewCode/actions/runs/41565098297) | 全量 1970/27；扩展专项 344；Docker 2 |
| U7 | 只读 Shell 并发/退出码 | 已推送，五作业成功 | `51c38c78aa3c565a8ff1bee45242772356aea08a` | [41570476588](https://github.com/pretextQ/MewCode/actions/runs/41570476588) | 全量 2048/29；Windows 新专项 78/2；Linux 专项 182 |

## 每项追加记录格式

在表格更新状态后，按对应 ID 追加简短记录：实际行为与兼容边界；专项及必要全量的命令、结果与平台；失败原因和修正；Ruff、双平台 mypy、工程守卫、锁文件/diff；提交、远端核对、对应 SHA 五作业 CI；未执行的真实端点/平台验收。默认关闭/实验功能与实际效果分开记录。

下一项可包含上一项的最终提交/CI 记录；最终项在交付答复核实自身 SHA，避免无意义的自引用提交循环。大体积原始日志保留本地，不强制加入全部 docs/evidence。

## 已知边界

- Windows/Linux 是本轮目标，不新增 macOS/iTerm2 待办。
- 没有真实模型预算；MCP native/tool_reference 和真实缓存收益不在本轮验收。
- 黑色 TUI 与历史终端实窗人工待验项保持独立。
- `docs/README.md` 原有一条本地旧审查入口改动继续留工作区，不夹带。
- 自定义命令、技能安装器、后台记忆整理、浏览器 UI 为后续方向。

以下实施记录按发生顺序保留；中途失败、补充验证与修正不能替代表首最终 SHA/CI。历史 U4 后暂停不覆盖本次明确恢复，当前状态以表首与最新交付记录为准。

## U1 实施记录

- 在共享 PermissionChecker 的文件路径门禁接入写保护；先于显式 allow、PLAN 写例外及模式放行。项目/全局三种配置及 skills 目录均保护，读取与专用规则持久化继续工作。路径同时比较名称和真实目标；Windows 常见别名归一化，链接解析失败拒绝。
- 技能管理审计：现有命令只列出、查看、重新加载并注册技能，没有安装器写路径，本轮不引入绕过开关。普通源文件、plans/memory/sessions 为正对照。安全作业加入新的保护测试文件。
- 专项（Windows/Python 3.14.4）：`uv run --locked pytest tests/test_protected_paths.py tests/test_permission_security.py -q --maxfail=3 --timeout=120` 初次修正后 109 passed / 1 skipped；补充 junction 与其余策略目标后保护专项 60 passed / 1 skipped。Windows junction 实测成功；本机 symlink 缺权限跳过，Linux 真实符号链接由对应 CI 覆盖。
- 失败事实：首轮红灯用例未绑定工具注册表，误将当前仓库 `.mewcode/config.yaml` 覆盖为测试文本并创建 `source.py`。已修正全部新测试为临时目录绑定并删除所创建的 source.py；配置未找到可确认备份，用户回复没有已知备份，无法原样恢复，未用示例配置替代。该事件不混作已满足“未读取/修改真实配置”约束。早期另一轮 CUSTOM 读权限等待已终止，新增明确读 allow 后重新通过。
- Ruff、win32/linux 目标 mypy（189 文件）、工程守卫、`uv lock --check` 通过；源码预算未提升。全量结果见下段，对应提交与成功 CI 见表首。

- 本地全量：`uv run --locked pytest -q --maxfail=10 --timeout=120`，1823 passed / 24 skipped，296.56 秒；该次收集后补充的 37 个边界用例已单独运行，保护专项最终 60 passed / 1 skipped（4.92 秒）。未改变产品实现；本项远端 CI 将覆盖最终测试集。规划提交 `6a6b25534072859c86de446a21a2b5a435685d7d` 的 push CI [41422081170](https://github.com/pretextQ/MewCode/actions/runs/41422081170) 五作业已核实成功。

## U2 实施记录

- 全局→最近 Git 根到运行目录→本地覆盖的共享 loader，增加 AGENTS.md、来源与真实路径去重。include 限各自 scope，围栏不展开，文件错误变成诊断；64 KiB 单文件/256 KiB 总字节/128 文件/5 层 include/64 级目录，超过预算整文件跳过。选择显式有界读取，不调用 Git 子进程。
- TUI、headless、service 原有入口复用共享 loader；独立子代理/隔离 worktree 新增调用，fork 保留上下文继承。pane bootstrap 标记已加载；旧 payload 在 worker 本地加载，避免双重注入。TUI 恢复 worktree 后按恢复目录重新取启动指令。
- 新增测试默认临时 home（与项目目录分离），HOME/USERPROFILE 同步传给子进程；显式 isolated_home 仍兼容原契约。该层不替代工具注册表的 work_dir 绑定。
- Windows/Python 3.14.4：指令/记忆专项 59 passed / 1 skipped；实际入口+真实 worker SDK 子进程 22 passed / 3 skipped；补充恢复目录与目录预算后指令专项 11 passed / 1 skipped。真实 TUI/headless/service/子代理脚本 LLM 请求、worker 本地 HTTP SDK 载荷均看到指令且后续轮次不重复。symlink 本机跳过，junction 越界实测拦截；tmux 集成未本地执行。
- 初次专项 1 failed（临时 home 与项目重合造成全局文件优先且去重，旧优先级用例因此失效），已改为默认独立 home；Ruff 的未使用导入问题已修正。工程检查通过（mypy 两平台各 190 文件），全量与远端最终结果已记在下段和表首。U1 远端 SHA 与本地一致，五作业成功后才开始 U2 产品改动。

- 首次全量 2 failed / 1868 passed / 25 skipped（298.87 秒）：默认隔离 home 位于临时沙箱，旧 `~/.ssh` 越界断言不再成立；创建空 home 目录影响原子写入的目录清理断言。改为不预建 home，越界用例显式使用无凭据的虚拟根目录路径。受影响专项最终 90 passed / 2 skipped；最终全量 `uv run --locked pytest -q --maxfail=10 --timeout=120`，1871 passed / 25 skipped（299.31 秒）。Ruff、双平台 mypy、工程守卫、锁文件、diff 检查均通过，未提高预算。指南：[项目指令](../../guides/project-instructions.md)。

## U3 实施记录

- 普通日志追加/轮转，独立 PID+会话诊断组保存 start/exit/异常堆栈。CLI wrapper 保留退出码，接入 service/headless/worker asyncio 异常处理、线程、Textual 及 TUI 消息异常入口；旧处理器继续执行，同异常对象去重。
- faulthandler 单独稳定描述符，安全 dump_traceback 在轮转前后实测；退出时 fault 尾部约 1 MiB，活动进程内不轮转、硬终止可能留未裁剪文件，详见[诊断指南](../../guides/diagnostics.md)。保留最近 20 组并用真实文件锁跳过活动组，清理验证绝对边界，不跟随链接删除。日志全部跟随 MEWCODE_LOG_FILE，目录不可用时降级。常见凭据清理、单条日志上限，不主动记录请求/配置/会话。
- Windows/Python 3.14.4：诊断/CLI/service/worker/TUI 专项 48 passed / 3 skipped（38.91 秒）；补充真实线程入口后诊断专项 8 passed / 1 skipped（8.62 秒）。两次真实 CLI 启动保留历史，异常 exit 1/7 不变，异常不重复，重定向不产生 worktree 日志，模拟路径不可用不改变 exit；本地未进行 POSIX chmod，留给 Linux CI。线程、Textual、异步入口均实测。
- 初轮工程检查的 import 顺序、win32 目标 fcntl 类型错误已修正，平台分支改为 sys.platform；Ruff、两平台 mypy 各 192 文件、守卫和锁文件通过。U2 对应五作业成功后才实施 U3，全量结果见下段，本项最终修正与成功 CI 见表首及最终交付节。

- 最终本地全量 `uv run --locked pytest -q --maxfail=10 --timeout=120`：1879 passed / 26 skipped（308.73 秒）；收集后补充的并发 PID 与跨范围清理探针已专项实测，最终诊断专项 10 passed / 1 skipped（11.28 秒）。最终绝对日志路径修正亦已专项覆盖；远端 CI 覆盖最终文件集。Ruff、双平台 mypy、工程守卫、锁文件及 staged diff 检查通过，未增加 ceiling。

- U3 收尾补充：主提交 `5378ced` 推送后审计发现 `_send_message` 外层异常仅显示错误，已增加同一诊断入口并以真实离线 TUI 验证。最终专项 11 passed / 1 skipped（18.00 秒）；两行诊断接线未改变控制流，复用上次全量并运行受影响端到端与工程检查，远端最终 SHA CI 覆盖累积源码。不拿主提交的 CI 代替补充提交。

## U4 实施记录

- PermissionReply 与共享规范化兼容旧 enum；DENY 才保留反馈，截断 1000 Unicode 字符、清理 ANSI/控制/方向字符，未知回复 fail closed。实际 Agent 拒绝结果为 error，反馈只进入对应 tool_result，无新增用户轮次/模型请求；非交互不发明反馈。
- No 选项显示 Input，输入/粘贴可用；超长粘贴按剩余长度截取，切换选项丢草稿。Escape 无反馈拒绝，Ctrl+C 复用取消；单次回复守卫、future.done 检查及原请求 future 身份绑定共同覆盖重复/迟到事件，输入恢复。App 优先级 Escape 查找聚焦输入的权限祖先，避免误取消整个 run。TUI 详情原有 markup=False，反馈按纯文本显示。
- Windows/Python 3.14.4：权限/配对/TUI 专项 124 passed（12.25 秒）；该阶段反馈专项 13 passed（6.88 秒），覆盖实际 Agent 拒绝保持文件不变，三客户端下一次 SDK payload 的 ID/反馈、并行 ask 不串草稿、结构化 ALLOW_ALWAYS 的真实持久化及无 UI 拒绝。安全 CI 加入新反馈专项。
- 首轮反馈专项 1 failed（Textual 超长粘贴被整段拒绝），已加有界 paste 处理再通过。早期现有 Grep 2ms 心跳断言出现一次 0 beats，未改动 Grep；同一完整专项有界重跑后 124 passed。初轮 Ruff 格式问题已修正。Ruff、双平台 mypy（193 文件）、工程守卫与锁文件通过，全量及本项最终 CI 见下方记录与表首。
- 用户在 U4 实施期间明确要求“u4完成之后，先暂停，我的额度不够了”。完成 U4 验证、提交推送及对应 CI 后暂停，U5–U7 不开始；恢复需用户后续指示。U3 最终补充提交五作业成功后才开始 U4。

- U4 取消竞态与窄屏补充：最终反馈专项 18 passed（8.31 秒），60×18/80×24 输入均可见；旧已取消授权框的迟到反馈不能回答下一请求。请求 token 初次 mypy 将 Any.future 窄化为 object，改用已有带类型 pending 字段后双平台均通过。最终 Ruff、双平台 mypy、工程守卫、锁文件通过。

- U4 首次全量 1 failed / 1894 passed / 26 skipped（322.68 秒）：旧 TUI details 用例直接断言 enum，现已通过共享 normalize_reply 断言新协议；相应 details+反馈专项 38 passed（16.33 秒）。最终全量 `uv run --locked pytest -q --maxfail=10 --timeout=120`：1900 passed / 26 skipped（322.56 秒），覆盖最终请求身份防护和窄屏测试。`uv build --no-sources` 成功，wheel 检查包含 U1–U4 的全部 5 个新模块。Ruff、双平台 mypy（193 文件）、守卫、锁文件、diff 检查通过；未提高 ceiling，无真实模型调用。该轮之后的 push CI 失败与修正见下文；最终 SHA 和成功 CI 见表首及最终交付节。

- U4 首次 push `d9423899f39193a8fd11d90fcb18b5076e01d19e` / CI [41433858498](https://github.com/pretextQ/MewCode/actions/runs/41433858498) 未通过：security 与 Linux 3.13 成功；Linux 3.11 出现权限框窄屏滚动及 orphan-bash 回收失败，Windows 3.11/3.13 出现反馈框挂载/聚焦时序失败。没有把其中绿灯当整体成功。反馈栏改为首次布局前隐藏，权限框滚动延至布局刷新后；离线交互测试等待实际焦点后发键，失败时也先取消并等待拥有的运行任务，避免 teardown 异常串扰。
- 修正验收：Windows 3.14 TUI/反馈/details 56 passed（21.82 秒）；隔离 Linux 3.12 同组加进程回收 77 passed（46.75 秒），orphan-bash 个案另连续五次均通过，未修改或放宽进程回收实现/断言，该 CI 单次失败原因未确认。最终冻结源码在临时 venv 的 Windows 3.11.9、3.13.13 同组各 56 passed（69.74/51.01 秒），隔离 Linux 3.12 同组 56 passed（22.81 秒）。不冒充本地已跑 Linux 3.11，仍需最终 SHA 的 CI 确认。临时快照首次因缺 README 无法构建 editable，补齐公开 README/LICENSE 后正常安装锁定依赖；未读取用户配置。
- 修正后 Ruff、双平台 mypy（193 文件）、工程守卫、锁文件/diff 再通过；复用既有 1900/26 本地全量并执行上述受影响平台专项，修正提交的 CI 负责完整最终源码跨平台回归。仍仅收尾 U4，最终 CI 全绿后按用户要求暂停。

- U4 第二次 push `a2238bac11a3139e2a5543112d54ded372e60722` / CI [41454802016](https://github.com/pretextQ/MewCode/actions/runs/41454802016)：security、Linux 3.13、Windows 3.13 成功；两个 Python 3.11 作业仅剩同一窄屏权限框位置断言失败，反馈输入和进程回收已通过。Textual scroll_visible 自身默认还会延迟一次；布局后回调改用 immediate=True，避免再次排队到后续刷新。Windows 3.11/3.13 最终同组各 56 passed（17.88/18.24 秒），3.14 同组 56 passed（17.83 秒），Ruff/双平台 mypy/守卫/锁文件通过。首次真正 Linux 3.11.17 隔离环境单跑旧失败用例通过（8.15 秒），说明单项并未复现整套 CI 的时序，不能把它写成复现后修复成功；继续用最终 SHA 的完整 CI 验证。

## U4 最终交付与暂停

- 最终有效提交 `f2ac5e192fe72dc9dcccda4d451f618743b2f9d5` 已正常推送 origin/master，远端 SHA 已核对一致；对应 push CI [41456722525](https://github.com/pretextQ/MewCode/actions/runs/41456722525) 的 Windows/Linux × Python 3.11/3.13 及 security 五作业全部 success。早期两个红灯 run 及 TLS 推送失败均保留历史，不替代最终结果；TLS 有界重试后推送恢复，没有改动持久 Git/TLS 配置或关闭证书校验。
- 最终冻结源码另跑隔离 Linux 3.11.17 全量：1882 passed / 43 skipped / 1 failed（217.72 秒），唯一失败是导出快照没有 .git，既有 test_e2e_rule_allows_git 的 git status 因此返回非零。为临时快照补 Git 初始化后同用例 1 passed（3.62 秒）。该环境问题未修改产品权限或进程回收，也不将这组本地命令冒称单次全绿；最终真实 CI 两个 Linux 作业均全绿。
- 本地 Windows 主全量 1900/26、最终修正后的 3.11/3.13/3.14 各 56 项专项、两平台 mypy、Ruff、守卫、锁文件、diff 及最终构建已通过。全程没有真实模型调用、真实用户 TUI 会话或新 native MCP 协议。源码预算未提升。
- 按用户最新要求在 U4 后暂停；U5–U7 未开始，必须等待明确恢复。此处记录最终产品 SHA/CI；后续纯文档更新不改写产品交付 SHA，也不冒充新的产品验证。原 docs/README.md 用户入口改动仍未提交。U1 隔离错误覆盖的本地配置无已知备份，仍未恢复，不将工作区描述为完全无损。
- 临时环境清理命令被自动审批策略拒绝（仅返回 blocked by policy，无具体原因），未执行删除；临时目录 C:/Users/32519/AppData/Local/Temp/mewcode-u4-linux-wzpqg9m2 与缓存卷 mewcode-u4-linux311-cache-01a19b9e 保留。所有本轮测试进程和 --rm 容器均已正常结束。

## 文档同步

2026-11-02：按用户后续要求同步仓库 README、docs 总入口、演进索引、计划、交接、Agent/架构指南与维护映射，移除当前入口的“规划阶段/等待 U4 CI”过期状态。保留全部历史数字、失败事实、配置未恢复与清理被拒记录；U5–U7 继续暂停。本次仅改 Markdown，沿用上述产品验证与 CI，不重跑模型或真实 TUI。

## U5 实施记录（2026-11-03 恢复）

- EditFile 实际成功写入、历史/缓存更新后调用独立分组 diff helper。完整比较的增删统计与预览截断分离；三行上下文和标准 hunk 起点/行数，无末尾换行明确标记。总预览最多 200 行/12000 字符，比较限前后合计 100 万字符/2 万行且行数乘积不超过 200 万；预算外明确统计省略，避免重复行导致无界比较。预览异常返回编辑已完成，不伪报写入失败。
- Windows/Python 3.14.4：`uv run --locked pytest tests/test_edit_diff.py tests/test_filehistory.py tests/test_rewind_cache.py tests/test_self_verify.py tests/test_tui_details.py tests/test_review_oct24.py -q --maxfail=3 --timeout=120`：164 passed（25.24 秒）。补充实际 Agent interactive/headless 下一请求及最终 hunk 起点修正后新专项 `tests/test_edit_diff.py`：16 passed（4.16 秒）；覆盖混合换行、UTF-8 BOM、Unicode、删除到空文件的真实字节与 rewind、读门禁/缓存、长输出/计算预算、成功后预览异常、写入失败、历史与实时纯文本展示。现有自验证观察器继续工作；未启动用户 TUI。
- 首轮测试曾因 pytest 自动生成超长参数 ID 触发 Windows 临时路径错误，另有错误引用不存在历史字段的断言；已用短 ID 和真实 snapshot/rewind 修正。首次 Ruff 行长与 helper 类型标注已修正，工程检查最终通过（Ruff、win32/linux mypy 各 194 文件、工程守卫、锁文件、diff）；不提高 ceiling，不读取配置/记忆。
- 本地全量 `uv run --locked pytest -q --maxfail=10 --timeout=120`：1916 passed / 26 skipped（324.50 秒）。全量启动后的 hunk 零行起点和展示过滤微调以最终受影响专项补验：`tests/test_edit_diff.py tests/test_tui_details.py tests/test_tui_redesign.py tests/test_tui_hardening.py` 59 passed（15.94 秒），覆盖 C1/方向控制字符；Ruff、双平台 mypy、工程守卫、锁文件与 diff 再通过。最终 SHA 的完整跨平台 CI 仍须核实，不能以此本地 Windows 结果替代。
- 最终 `d456498e6bd7bda9951e229edce128b5a0fd6681` 已推送并核对 origin/master 一致；push CI [41560096587](https://github.com/pretextQ/MewCode/actions/runs/41560096587) Windows/Linux × Python 3.11/3.13 和 security 五作业全部 success 后，才开始 U6 产品修改。

## U6 实施记录（2026-11-03）

- 保留 legacy 默认与各入口原有可见性；初次连接后选择 eager/dispatch/显式 auto（紧凑 UTF-8 schema 字节/4 向上取整，阈值 min(8192, context_window/10)，超过选 dispatch）。顶层与 service 独立配置，服务直跑/容器仍过滤非 readOnlyHint；配置可显式覆盖回 legacy。公开契约：[MCP 加载指南](../../guides/mcp-loading.md)。
- MCPCall 在 Agent 既有执行门禁之前解析真实 MCP 身份，同一管线处理权限/反馈、启停、hooks、完整参数验证与监督；只保留外层 ID，不重复执行事实。未知/禁用/过滤/非 MCP/递归目标拒绝，碰撞封锁歧义身份。注册表复制重绑 ToolSearch/MCPCall 并继承禁用状态。dispatch 入口和目标保守串行；没有 native/beta header。
- 实施前合成 0/5/250 工具基线：legacy tools 紧凑 UTF-8 字节 404→404 / 404→2234 / 404→91904。新模式同样样本：eager 404/2234/91904 搜索前后不变，dispatch 404/799/799 不变；schema 总字节 2/1831/91501。三客户端 SDK 请求捕获验证实际请求集合与配对，不声称真实缓存/费用收益。
- Windows/Python 3.14.4：初始既有 MCP/服务/子代理/配对回归 143 passed（19.48 秒）；扩展 `tests/test_mcp_loading.py tests/test_tool_search.py tests/test_mcp.py tests/test_mcp_concurrency.py tests/test_service_mcp.py tests/test_subagent.py tests/test_permission_security.py tests/test_permission_feedback.py tests/test_request_pairing.py tests/test_service_sandbox.py` 344 passed（55.18 秒）。全量参数保留调整后 MCP 新专项+既有 MCP/并发 94 passed（18.56 秒）；最后连接代际/旧栈收尾补充后的 loading+并发专项 59 passed（19.01 秒）。实际入口六组合（TUI/headless/service × legacy/dispatch）、真实 stdio 成功/失败/超时取消/重连/回收、真实目标 hooks 一次性与监督、严格嵌套 schema/null/额外字段及 SDK ID 均覆盖。
- Docker daemon 起初未运行，后台启动 Docker Desktop 后可用。真实独立 Linux/Python 3.12 容器，`MEWCODE_DOCKER_TESTS=1 uv run --locked pytest tests/test_sandbox_live.py -k agent_runs_in_container_with_mcp_tools -q --maxfail=2 --timeout=900`：2 passed / 15 deselected（22.36 秒）；本地脚本 LLM + 假 Loki + 真实 MCP 与 CLI，dispatch 经搜索再调用、tools 稳定、真实目标计数准确，容器无残留。该轮在最后连接代际微调前运行，后续受影响专项及最终 CI 覆盖收尾；未调用真实端点。
- 失败事实：新测试初期错误要求 eager 搜索返回已直接可见 schema、AgentDef 字段名称用错、临时配置缺 provider、ConfigError 误按 ValueError 捕获，均修正为实际 API 与行为；产品门禁未为测试放宽。首次离线锁解析改选旧版本，已立即恢复原锁定包与环境，仅保留 root 对已有 jsonschema 的两个依赖元数据条目，最终锁检查成功；没有升级/降级包版本交付。Ruff/import/类型及行长初轮问题均已修正。
- 首次全量 4 failed / 1966 passed / 27 skipped（349.42 秒），均为旧 MCP 角色测试使用仅含 name 的 SimpleNamespace，不具备新增注册表重绑 hook；注册入口保留旧替身兼容，真实 ToolSearch/MCPCall 仍必须重绑。随后角色/MCP/监督/自验证修正专项 198 passed（34.97 秒），补齐 tool_call 也按真实目标监督、不记录外层两次。
- 最终本地全量 `uv run --locked pytest -q --maxfail=10 --timeout=120`：1970 passed / 27 skipped（333.63 秒）。全量启动后的 MCP 状态数量/名称展示修正以 `tests/test_mcp_loading.py tests/test_commands.py` 补验，95 passed（18.48 秒）；状态只数真实启用目标，不把 dispatcher 当服务工具，也不混淆含下划线的 server。最终 real_stdio 补充检查重连前后两个 PID 都退出，并将所有断言放进 finally 收尾范围。Ruff、win32/linux 目标 mypy 各 196 文件、工程守卫、锁文件、diff、四项缺陷回植绿→红检验通过，源码预算未提高。
- 提交前 Schannel 查询三次握手失败；单次命令使用 OpenSSL 后端后成功核对 origin/master 仍为 U5 SHA，未修改持久配置、远端 URL 或关闭证书校验；一次公开 API EOF 也未当通过。推送与对应最终 SHA 的五作业 CI 待实际核验。
- U6 最终 `61290369531dbb06e1297efd08dca14ea0cb7b1d` 正常推送 origin/master，首次 OpenSSL 推送也遇 TLS EOF，单次 OpenSSL+HTTP/1.1 重试后成功并核对远端。push CI [41565098297](https://github.com/pretextQ/MewCode/actions/runs/41565098297) attempt 1：四作业 success，仅 Linux 3.13 的旧 `test_timeout_reaps_descendants[tree-service]` 在 2 秒检查后检测存活 PID（1952 passed / 44 skipped / 1 failed）。新增 MCP 测试均通过，未修改或放宽进程树实现/断言。
- 冻结 SHA 独立 Linux/Python 3.13.16（CI 为 3.13.18）进程回收套件 21 passed（25.36 秒），原失败用例连续五次各 1 passed（4.99/4.94/4.88/5.86/4.86 秒），没有复现。对原失败作业请求一次有界重跑后，attempt 2 最新五作业全部 success，run 的 event=push / head_sha / conclusion 已再次核实；只在此后开始 U7。不能将未复现写成已定位并修复，保留原因未确认的瞬态记录。
- 新建隔离快照 `C:/Users/32519/AppData/Local/Temp/mewcode-u6-linux313-3k6r1y4p` 与缓存卷 `mewcode-u6-linux313-cache` 用于本轮，未触碰 U4 遗留目录/卷。测试 --rm 容器已退出，没有清理未经核实的用户产物。

## U7 实施记录（2026-11-03）

- Tool 增加按参数判断接口，默认仅兼容真实 bool True，不把方法对象真值当放行。共享分批与拥有任务收尾接入 interactive/headless/service 使用的实际 Agent 路径；相邻查询并发，WriteFile/EditFile/普通命令为屏障，结果与回调按原提交顺序和 ID 归位，MCPCall 与 dispatch 目标继续串行。pre hook 拒绝不再进入 parallel post hook，与串行一致；每次权限、hooks、监督、work_dir 及请求未知结果修复保留。
- 首期白名单限 POSIX pwd/单相对文件 cat、Windows 无参数 cd/单相对文件 type，以及受控 git status 选项。严格 ASCII 词法/参数检测，未知语法和 Shell 组合/展开等均串行；未改权限自动放行规则。Git status 真实 argv 路径复用 TUI 的 filter 校验：只枚举键名、限制规模与预检耗时，禁 filter/fsmonitor/pager/外部 diff/optional locks，忽略子模块内部状态，检查失败不退回普通 Shell。公开范围见[权限语义](../../guides/permission-semantics.md)和[Agent 契约](../../guides/agent-runtime.md)。
- 正常 Shell 结果在开头明确数值 Exit code；退出 1 仅对严格单条 rg/grep 无匹配、diff 有差异作为非错误，其余非零与测试失败仍错误。超时保留实际回收状态并标明 termination，预检/启动失败无法取得退出码时明确 unavailable，不伪造 0；原 600 秒、2 MiB 合计捕获、编码回退与进程树清理不放宽。
- Windows/Python 3.14.4：初始 Agent/权限/回收/TUI/MCP 回归 212 passed（47.08 秒）。最终新专项 `uv run --locked pytest tests/test_shell_concurrency.py -q --maxfail=3 --timeout=120` 78 passed / 2 skipped（14.00 秒），rg 真实执行通过，缺 grep/diff 而跳过。受控事件记录 actual Agent 两个查询同时在途、写入与普通命令屏障、保序结果及临时 work_dir 的真实 Shell 输出；不是仅比较墙钟，不提供提速百分比。并行 ask/deny/反馈、真实 pre/post hook 与监督、双次取消及三个 SDK 的未知结果配对均覆盖。取消用例的进程工厂将固定查询替换为真实测试进程树，仅验证拥有/取消；普通 Shell 语义另以真实 COMSPEC 验证。
- 隔离 Linux/Python 3.13.16、Git 2.47.3：`tests/test_shell_concurrency.py tests/test_process_cleanup.py tests/test_mcp_loading.py tests/test_permission_feedback.py tests/test_request_pairing.py` 182 passed（45.29 秒），包含真实 /bin/sh、rg/grep/diff、恶意 Git filter/fsmonitor/pager 禁用与索引字节不变、MCP 与取消收尾；未将此平台冒称为本地 Linux 3.11/3.13.18。新临时快照 `C:/Users/32519/AppData/Local/Temp/mewcode-u7-linux313-xbcf30gh`，复用本轮 U6 缓存卷，容器已 --rm 退出。
- 失败事实：首次专项将不支持的 ask 写入规则、并误认为 reject hook 依脚本退出码条件拒绝；Windows 初始扩展专项 2 failed / 403 passed / 2 skipped（72.64 秒），首次全量 2 failed / 2046 passed / 29 skipped（350.50 秒）。改为仅对指定查询替换安全命令探针以触发真实 Checker 的 DEFAULT ask，hook 用真实匹配条件分流；产品权限不放宽。Linux 首轮因同样测试设置 3 failed / 56 passed；设置修正后又有新 headless 取消立即检查与旧 tree-service 取消的回收断言失败（2 failed / 180 passed）。新取消测试改用现有套件相同 2 秒退出宽限；旧回收断言和实现保持，仅加入失败时 PID/状态/父进程/组/会话诊断，不读取 argv/env。最终 182 项通过，旧用例未复现的原因仍未确认，不声称修复旧根因。
- 最终本地全量 `uv run --locked pytest -q --maxfail=10 --timeout=120`：2048 passed / 29 skipped（344.11 秒）。Ruff、win32/linux 目标 mypy 各 199 文件、工程守卫、锁文件与 diff、四项缺陷回植绿→红检验通过。`uv build --no-sources` 成功，wheel 含 U5–U7 全部 6 个新增模块。未提高 ceiling（Agent 1513/1575，App 1774/2100）；没有真实模型调用、真实用户 TUI 或真实配置/记忆读取。
- U7 首次推送 `8524860b5b9db06f3409e6bd2592ebe57774aeb3`，远端核对一致；push CI [41569156313](https://github.com/pretextQ/MewCode/actions/runs/41569156313)：Linux 3.11/3.13、Windows 3.13 和 security 成功，仅 Windows 3.11 的新 headless hooks 日志计数失败（2051 passed / 25 skipped / 1 failed）。两个并发 hook 原来共享单一追加日志，CI 记录少了 first/pre，而 second/pre/post 存在；未本地复现，不据此认定产品漏调。验收日志改为每目标独立文件，避免 Windows 共享追加竞争，并增加脚本完成标记；次数断言保留，产品代码未为此变更。3.14 最终新专项 78/2（15.36 秒），3.11/3.13 隔离验收与修正 SHA 的 CI 继续核验，不能用原四个绿灯替代。
- 追加修正仅测试记录与本段文档：隔离 Windows 3.11.9 / 3.13.13，新专项各 78 passed / 2 skipped（55.21 / 54.92 秒），固定依赖且项目主环境仍为 3.14.4。原完整产品全量与工程检查沿用，Ruff/diff 再通过；修正提交的五作业 CI 负责最终累计测试集。未在本地复现旧共享日志丢项，不将消除该测试竞争描述为已证明的产品 hooks 缺陷修复。
- 最终验收提交 `51c38c78aa3c565a8ff1bee45242772356aea08a` 已推送并核对 origin/master 一致；其 push CI [41570476588](https://github.com/pretextQ/MewCode/actions/runs/41570476588) attempt 1 的 Windows/Linux × Python 3.11/3.13 及 security 五作业全部 success，run 的 head_sha/event/conclusion 逐一核实。早期 `8524860` 红灯保留，不以其他 SHA 绿灯代替。

## U5–U7 最终交付与文档收尾

三项按 U5→U6→U7 独立提交推送，前项最终五作业成功后才开始后项产品修改；全部最终产品/验收 SHA 与对应 CI 见表首。基线文档 `7a08ec0` 的 CI 41459756578 也已核实成功，U1–U4 与 A1–A5 未重做。本段纯文档收尾不替代各项产品 SHA/CI，其自身远端和自动 CI 在最终交付答复核实，避免无限追加自引用提交。

保留 docs/README.md 原有旧本地审查入口，只有本轮状态 hunk 被暂存；其他用户改动不 reset/stash、不强推。没有真实模型调用、用户 TUI 会话、native/tool_reference 或 beta header；没有读取真实 MewCode 配置/记忆，U1 旧配置事故仍无已知备份、未恢复。Windows 缺 grep/diff 的两项 skip、真实终端人工待验与旧 Linux 回收失败原因未确认均保留。验收临时目录/新缓存卷保留，测试进程与 --rm 容器已退出；未触碰 U4 遗留产物，不扩展任何后续能力。
