# 参考库能力借鉴逐项交付台账

日期：2026-11-02。执行规范与完整验收：[11 号开发计划](evolution/11-upstream-borrowing-plan.md)。原始能力比较：[对照报告](review/review-2026-11-02-current-borrowing.md)。

用户已授权新会话按 U1 → U2 → U3 → U4 → U5 → U6 → U7 连续实施，每项完成即单独提交推送，确认对应 CI 成功后再开始下一项。实施中；每项结果与边界见下方记录。

| ID | 项目 | 状态 | 最终提交 / 远端 SHA | 对应 CI | 本轮验收 |
| --- | --- | --- | --- | --- | --- |
| U1 | 受保护配置/技能禁写 | 已推送，五作业成功 | `c81aeb60fc0d4557c7318b1addc96f50cdd553a8` | [41424253094](https://github.com/pretextQ/MewCode/actions/runs/41424253094) | 全量 1823/24；保护专项 60/1 |
| U2 | AGENTS.md/目录链/include | 本地完成，待本项 CI | 本项提交 | 待推送后核验 | 全量 1871/25；指令专项 11/1 |
| U3 | 崩溃日志/保留诊断 | 待实施 | — | — | — |
| U4 | 拒绝授权附带反馈 | 待实施 | — | — | — |
| U5 | 编辑后有界 diff | 待实施 | — | — | — |
| U6 | MCP eager/dispatch | 待实施 | — | — | — |
| U7 | 只读 Shell 并发/退出码 | 待实施 | — | — | — |

## 每项追加记录格式

在表格更新状态后，按对应 ID 追加简短记录：实际行为与兼容边界；专项及必要全量的命令、结果与平台；失败原因和修正；Ruff、双平台 mypy、工程守卫、锁文件/diff；提交、远端核对、对应 SHA 五作业 CI；未执行的真实端点/平台验收。默认关闭/实验功能与实际效果分开记录。

下一项可包含上一项的最终提交/CI 记录；最终项在交付答复核实自身 SHA，避免无意义的自引用提交循环。大体积原始日志保留本地，不强制加入全部 docs/evidence。

## 已知边界

- Windows/Linux 是本轮目标，不新增 macOS/iTerm2 待办。
- 没有真实模型预算；MCP native/tool_reference 和真实缓存收益不在本轮验收。
- 黑色 TUI 与历史终端实窗人工待验项保持独立。
- `docs/README.md` 原有一条本地旧审查入口改动继续留工作区，不夹带。
- 自定义命令、技能安装器、后台记忆整理、浏览器 UI 为后续方向。

## U1 实施记录

- 在共享 PermissionChecker 的文件路径门禁接入写保护；先于显式 allow、PLAN 写例外及模式放行。项目/全局三种配置及 skills 目录均保护，读取与专用规则持久化继续工作。路径同时比较名称和真实目标；Windows 常见别名归一化，链接解析失败拒绝。
- 技能管理审计：现有命令只列出、查看、重新加载并注册技能，没有安装器写路径，本轮不引入绕过开关。普通源文件、plans/memory/sessions 为正对照。安全作业加入新的保护测试文件。
- 专项（Windows/Python 3.14.4）：`uv run --locked pytest tests/test_protected_paths.py tests/test_permission_security.py -q --maxfail=3 --timeout=120` 初次修正后 109 passed / 1 skipped；补充 junction 与其余策略目标后保护专项 60 passed / 1 skipped。Windows junction 实测成功；本机 symlink 缺权限跳过，Linux 真实符号链接由对应 CI 覆盖。
- 失败事实：首轮红灯用例未绑定工具注册表，误将当前仓库 `.mewcode/config.yaml` 覆盖为测试文本并创建 `source.py`。已修正全部新测试为临时目录绑定并删除所创建的 source.py；配置未找到可确认备份，用户回复没有已知备份，无法原样恢复，未用示例配置替代。该事件不混作已满足“未读取/修改真实配置”约束。早期另一轮 CUSTOM 读权限等待已终止，新增明确读 allow 后重新通过。
- Ruff、win32/linux 目标 mypy（189 文件）、工程守卫、`uv lock --check` 通过；源码预算未提升。全量及提交 CI 结果在完成后补记。

- 本地全量：`uv run --locked pytest -q --maxfail=10 --timeout=120`，1823 passed / 24 skipped，296.56 秒；该次收集后补充的 37 个边界用例已单独运行，保护专项最终 60 passed / 1 skipped（4.92 秒）。未改变产品实现；本项远端 CI 将覆盖最终测试集。规划提交 `6a6b25534072859c86de446a21a2b5a435685d7d` 的 push CI [41422081170](https://github.com/pretextQ/MewCode/actions/runs/41422081170) 五作业已核实成功。

## U2 实施记录

- 全局→最近 Git 根到运行目录→本地覆盖的共享 loader，增加 AGENTS.md、来源与真实路径去重。include 限各自 scope，围栏不展开，文件错误变成诊断；64 KiB 单文件/256 KiB 总字节/128 文件/5 层 include/64 级目录，超过预算整文件跳过。选择显式有界读取，不调用 Git 子进程。
- TUI、headless、service 原有入口复用共享 loader；独立子代理/隔离 worktree 新增调用，fork 保留上下文继承。pane bootstrap 标记已加载；旧 payload 在 worker 本地加载，避免双重注入。TUI 恢复 worktree 后按恢复目录重新取启动指令。
- 新增测试默认临时 home（与项目目录分离），HOME/USERPROFILE 同步传给子进程；显式 isolated_home 仍兼容原契约。该层不替代工具注册表的 work_dir 绑定。
- Windows/Python 3.14.4：指令/记忆专项 59 passed / 1 skipped；实际入口+真实 worker SDK 子进程 22 passed / 3 skipped；补充恢复目录与目录预算后指令专项 11 passed / 1 skipped。真实 TUI/headless/service/子代理脚本 LLM 请求、worker 本地 HTTP SDK 载荷均看到指令且后续轮次不重复。symlink 本机跳过，junction 越界实测拦截；tmux 集成未本地执行。
- 初次专项 1 failed（临时 home 与项目重合造成全局文件优先且去重，旧优先级用例因此失效），已改为默认独立 home；Ruff 的未使用导入问题已修正。工程检查通过（mypy 两平台各 190 文件），全量与远端结果后补。U1 远端 SHA 与本地一致，五作业成功后才开始 U2 产品改动。

- 首次全量 2 failed / 1868 passed / 25 skipped（298.87 秒）：默认隔离 home 位于临时沙箱，旧 `~/.ssh` 越界断言不再成立；创建空 home 目录影响原子写入的目录清理断言。改为不预建 home，越界用例显式使用无凭据的虚拟根目录路径。受影响专项最终 90 passed / 2 skipped；最终全量 `uv run --locked pytest -q --maxfail=10 --timeout=120`，1871 passed / 25 skipped（299.31 秒）。Ruff、双平台 mypy、工程守卫、锁文件、diff 检查均通过，未提高预算。指南：[项目指令](../guides/project-instructions.md)。
