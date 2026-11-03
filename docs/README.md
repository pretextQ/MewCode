# MewCode 当前状态与文档入口

2026-11-03 当前交付：参考库借鉴 **U1–U7 已逐项实现**，U5 编辑 diff、U6 MCP eager/dispatch、U7 只读 Shell 并发/退出码均接入实际入口。U1–U6 已核实各项五作业 CI 成功；U7 最终提交的核验结果以[逐项台账](stage-2026-11-02-borrowing.md)和交付答复为准。设计见[11 号计划](evolution/11-upstream-borrowing-plan.md)，历史暂停、失败修正、平台结果及未解决事项继续保留。

公开行为见[权限语义](../guides/permission-semantics.md)、[Agent 契约](../guides/agent-runtime.md)、[MCP 加载](../guides/mcp-loading.md)、[项目指令](../guides/project-instructions.md)、[进程诊断](../guides/diagnostics.md)和[TUI 指南](../guides/tui.md)。本轮与历史全量数字分列于台账；原配置未恢复、Linux 回收用例未复现的失败及临时环境保留均如实记录，不将离线接线或 CI 成功等同于真实模型效果。

平台适用范围：按用户确认，项目以 Windows/Linux 的实际需求验收；不在 macOS 使用，macOS/iTerm2 不列为待办或交付缺口。

2026-11-02 A1–A5 修复入口：[实施计划](evolution/10-review-a1-a5-fix-plan.md)、[逐项验收与提交台账](stage-2026-11-02-a1-a5.md)、[运行契约](../guides/agent-runtime.md)。按 A5→A3→A1→A2→A4 独立实现、验收和提交推送；每项五作业 CI 核验及最终 SHA 以台账、Git/Actions 和交付答复为准。原终端实窗人工待验项独立保留。

2026-11-02 终端增量：Windows ConPTY 与 Linux POSIX PTY 各 32 项协议集成检查通过，Windows 替身异常退出后的父启动器恢复另 6 项通过，TUI 回归 47 passed。使用真实 OS 驱动，无模型调用；未完成实窗人工字体/鼠标选择/缩放重绘验收，不能将待办 1 标为全部完成。可复跑入口、失败修正与人工清单见[终端验收](stage-2026-11-02-terminal.md)。原有本地参考库审查入口改动保留。

2026-11-01 最新配色偏好：用户验收新版后要求黑色背景。默认主题已改为近黑主背景、浅色正文、紫色强调，双层顶栏与卡片布局保留；45 项 TUI 专项和 Ruff、win32 mypy 通过。用户已查看并认可实际黑色版，TUI 已停止。本轮提交交付包含 Git 预览安全补强；最终全量 1746 passed / 19 skipped，47 项专项、两平台类型检查及工程检查通过，提交核验结果见本轮记录。见[本轮记录](stage-2026-11-01-tui.md)；下文浅色设计及全量数字按阶段保留。

2026-11-01 视觉纠偏（优先于下文初版数字）：用户实际终端截图表明初版暗色单行顶部未对齐浅色参考图。现已修正为独立产品/版本栏、项目/分支/右侧模型栏、浅色输入卡片和常驻任务区域，并修复 Textual 主题响应属性被类属性遮盖的问题。专项 45 passed，视觉纠偏全量 1744 passed / 19 skipped（268.24 秒）；新增 Windows 父 shell 退出复位启动器专项另 1 passed。最终工程结果与异常终端恢复记录见[本轮报告的视觉纠偏节](stage-2026-11-01-tui.md)。真实 TUI 已按用户指令停止，后续只用离线验证，未重新启动模型会话。

2026-11-01 最新增量：用户已授权按 [TUI 计划](evolution/09-tui-redesign-plan.md)恢复开发，A 基础展示/交互与 B 按需详情已实现。用户消息用 `›`、底色、边线区分，助手有轻量标记；固定输入/状态、离底阅读、取消收尾、共享流式/历史组件和 F2 详情已接线。详情仅显示已有任务/执行事实和按需有界 Git 数据。使用方法见 [TUI 指南](../guides/tui.md)，本轮检查、失败修正和真实终端人工烟测限制见[验收记录](stage-2026-11-01-tui.md)；下列数字仍属于历史阶段。本轮最终全量 **1741 passed / 19 skipped**（264.39 秒），TUI 专项 **42 passed**；Ruff、两平台 mypy、工程守卫、四项回植、86/86 组件、锁文件及构建通过。

2026-11-01 最新增量（优先于下列历史状态）：intent anchor 首期已实现，复用默认关闭的监督开关。明确的本次任务目标在累计 20 个响应轮、距用户输入至少 10 轮后，可在下一次原额度请求中提醒；冷却 10 轮，每 run 最多三条。TUI 原始用户文本、headless/service 的显式 task 和 steer 是来源，不从模型摘要或历史猜目标。只确认送达，无采纳/防漂移效果结论；完整契约和本轮验收见[本轮报告](stage-2026-11-01-intent-anchor.md)与[监督指南](../guides/supervision.md)。下列数字保留为各阶段历史结果。

本轮最终全量 **1704 passed / 19 skipped**（259.92 秒），监督与 TUI 专项 **181 passed**（本轮新增 37 项），真实独立 Docker **16 passed**（83.49 秒）。Ruff、win32/linux 目标全包 mypy 各 174 文件、工程守卫、四项回植、锁文件、最终源码构建及组件 86/86 通过；初期测试错误和跳过原因保留在本轮报告。对应提交的远端 SHA/CI 以交付答复核验为准。

2026-11-01 最新增量：self-verify 首期已实现，复用默认关闭的监督开关。成功的内置 Python 文件编辑后，在下一次原额度请求中建议适用验证；只读/纯文档任务不触发，无适用测试时解释证据与限制，不增加请求。尝试、工具成功和覆盖 unknown 分开记录。范围、生命周期与验收见[本轮报告](stage-2026-11-01-self-verify.md)和[监督指南](../guides/supervision.md)。下列数字是历史阶段验收，不替代本轮结果。

本轮最终全量 **1667 passed / 19 skipped**（250.78 秒），监督专项 **141 passed**（88 项新增），独立 Docker **16 passed**（最终源码接线另单独 1 passed）；Ruff、win32/linux 目标 mypy 各 173 文件、工程守卫、四项回植、锁文件、构建及组件脚本 86/86 均通过。跳过原因及初期失败保留在本轮报告；推送与对应远端 CI 以交付答复核验为准，无真实模型效果或生产收益结论。

2026-11-01 下一阶段：dead-end 服务反馈已实现，随 `service.enable_supervision` 默认关闭。完整 pytest 失败证据连续相同时，只向现有预算允许的下一次修复追加重新诊断建议；未知格式、失败变化、可能 TDD、环境/超时和 execution 重置分别记录。见[本轮验收](stage-2026-11-01-dead-end.md)与[监督指南](../guides/supervision.md)。本轮无付费模型调用、远端修复 PR 或生产部署；验证行为不等于生产收益。
本轮最终全量 **1579 passed / 19 skipped**（246.11 秒），新增专项 **48 passed**，独立 Docker **16 passed**；Ruff、Windows/Linux 目标 mypy（各 172 文件）、工程守卫、四项旧缺陷回植、锁文件、构建及组件脚本 86/86 通过。远端交付与 CI 以所属提交及交付答复的核实为准。

2026-11-01：按研究会话中新授权，优先实施通知 Outbox、策略快照与恢复重验证、轻量执行记录。
本轮本地交付和验证见[服务可靠性验收](stage-2026-11-01.md)，行为见[服务可靠性指南](../guides/service-reliability.md)。
该服务可靠性阶段当时将 dead-end 顺延，现已在文首所述下一阶段实现；GitHub 事件接入和 observe 模式仍待设计，下列历史验收数字保留。

2026-10-31 增量：执行循环预算预警已接入交互、headless 和服务共用内核，随监督开关默认关闭。全量 1495 passed / 19 skipped；独立 Docker 16 passed。当前交付与检查见[最新验收](stage-2026-10-31.md)，行为见[监督指南](../guides/supervision.md)。下列 2026-10-30 数字为历史结果；“无 detector”是当时边界。


2026-10-30 下一阶段更新：A 审查范围及语义评估已实现，真实候选对比未证明效果提升；B 最小监督内核已实施。见[新增验收](next-stage-2026-10-30.md)。历史验收数字保留。
更新：2026-10-30。版本 0.3.0 / MIT；历史验收基线 `4cef69f`，本期代码起点 `a85444c`。

**A 审查范围/语义评分、B 最小监督内核及执行循环预算预警已实施，默认关闭。真实模型候选对比未证明质量提升；监督预警没有生产效果证据。**

## 先看这三份

1. [当前交付台账](stage-2026-11-02-borrowing.md)：U1–U4 最终提交/CI、验证边界与暂停点；[A1–A5 台账](stage-2026-11-02-a1-a5.md)保持独立。[intent anchor 验收](stage-2026-11-01-intent-anchor.md)、[self-verify 验收](stage-2026-11-01-self-verify.md)、[dead-end 验收](stage-2026-11-01-dead-end.md)、[服务可靠性验收](stage-2026-11-01.md)、[循环预算预警验收](stage-2026-10-31.md)、[上期实施验收](next-stage-2026-10-30.md)与[历史验收](acceptance-2026-10-30.md)保持原数字。
2. [下次工作入口](next-session-prompt.md)：剩余事项及推荐顺序。
3. [公开架构说明](../guides/architecture.md)、[监督指南](../guides/supervision.md)与[独立审查指南](../guides/independent-review.md)：运行行为、开关、证据和预算。

## 本期增量验收

全量 **1475 passed / 19 skipped**（232.62 秒）；独立 Docker **16 passed**（72.17 秒）。Ruff、Windows/Linux 目标全包 mypy（各 167 文件）、锁文件、工程守卫、四项回植、子代理 86/86 与构建通过。A `c653b77`、B `37b74ef` 已推送，各自五个远端 CI 作业全通过。跳过和曾失败的修复过程详见本期报告；不把默认关闭的监督基础计作效果改善。

## 历史验收基线（本期增量见文首）

| 项目 | 2026-10-30 结果 |
| --- | --- |
| 全量测试 | 1439 passed / 18 skipped，246.91 秒 |
| Docker 真机专项 | 单独启用后 15 passed，111.80 秒；不与全量结果相加当作单轮套件数 |
| Ruff、全包 mypy | 通过；Windows/Linux 目标各 164 源文件 |
| 锁文件、工程守卫、缺陷回植 | 通过；四项旧行为分别命中 2 / 5 / 5 / 1 个预期断言失败 |
| 子代理、依赖审计、打包 | 86/86；未发现已知依赖漏洞（本包审计跳过）；sdist/wheel 成功 |
| 基线远端 CI | Windows/Linux × Python 3.11/3.13 及安全作业全通过 |
| deepseek-flash 真实审查 | 三个有效正例、一个正确负例、一个部分覆盖下的错误推断 |

历史五次模型调用保守估计 0.06148 元；本期新增十次 0.136408 元，累计 0.197888 元，低于 1 元授权；不是账单金额。大 diff 样例不能计为有效检出，不采用未经意见复核的 100% 评分。

## 功能进度

| 工作线 | 状态 | 边界 |
| --- | --- | --- |
| CLI/TUI 与共享 Agent 内核 | 已实现 | 本次没有人工 TUI 交互验收 |
| M1 告警 → 修复 → PR → 人工审查 | 已实现，有历史真实演示 | 本次未创建远端修复 PR；测试和 CI 按配置执行 |
| M2 Docker / Compose / Skills / 只读 MCP | 已实现，本次真机专项通过 | 容器内 Agent 测试使用假 LLM；第三方工具声明仍需信任判断 |
| M3 指标 / 多仓策略 / 回放 | 已实现 | 历史回放早于后续修复；没有生产收益证据 |
| 服务可靠性：通知 / 策略 / 执行记录 | 已实现，交付版本见 Git 记录 | 单进程 SQLite；通知不保证恰好一次，旧事件不回填，最终检查见最新验收 |
| dead-end 服务反馈 | 已实现，默认关闭 | pytest 契约与结构化证据；仅重新诊断建议，未知/TDD/环境/超时不认定重复失败 |
| self-verify 首期 | 已实现，默认关闭 | 内置 Python 编辑与受支持 Bash 验证意图；尝试/工具成功/覆盖 unknown 分开，每 run 最多一条建议 |
| intent anchor 首期 | 已实现，默认关闭 | 显式目标摘录、响应轮/用户静默门槛与冷却，每 run 最多三条；无 expect，不证明遵守目标 |
| 早期 42 项、R1–R5、R01–R09、N01–N41 | 已完成 | 原始报告用于追溯，不是待修清单 |
| 阶段 1：最小工程守卫 | 已完成 | 四项定点回植，不代表全部历史缺陷覆盖 |
| 阶段 2：独立审查首期 | 已完成，默认关闭、仅提供意见 | 动态上下文、多轮分块、自动返工未实现；效果验收有限通过 |
| 阶段 3：监督内核、循环预算预警、self-verify 与 intent anchor | 已实现，默认关闭 | 事件、run/turn、总线、TTL、去重、通道预算、送达/expect；无 token 预警 |

## 深入阅读

- [已授权的下一阶段实施计划](evolution/08-next-stage-plan.md)：先修正局部 diff 审查及效果评估，再实现最小监督内核；A/B 首期已实施，验证见本期报告。

- [演进与验证证据](evolution/README.md)：里程碑与历史回放口径。
- [参考库借鉴计划](evolution/11-upstream-borrowing-plan.md)与[交付台账](stage-2026-11-02-borrowing.md)：U1–U4 已交付，U5–U7 暂停；[原始对照报告](review/review-2026-11-02-current-borrowing.md)保留实施前建议与离线探针，不作为当前未实施清单。
- [PR 审查路线](evolution/05-pr-review-improvements.md)：首期边界和后续扩展。
- [监督闭环设计](evolution/06-tianshu-supervision-notes.md)：首期已实现；后续 detector 待实施。
- [测试体系设计与实施边界](evolution/07-tianshu-test-notes.md)：最小守卫已完成，其余是提案。
- [维护记录与提交映射](../guides/maintenance.md)：历次整改证据。
- 本地辅助：[项目定位](evolution/01-positioning.md)、[架构补充](evolution/02-architecture.md)、[M1](evolution/03-m1-alert-driven.md)、[M2](evolution/04-m2-enterprise-capability.md)、[测试环境](fixes/test-environment.md)、[后续事项](fixes/backlog.md)。
- 本地整改档案：[修复总览](fixes/README.md)、[R1–R5](fixes/review-2026-10-08-plan.md)、[N01–N41 完成记录](fixes/review-2026-10-24-fixes.md)、[原始审查](fixes/review-2026-10-24-full.md)。
- [历史档案](history/README.md)：保留当时记录，不作为当前操作说明。

## 文档约定

当前进度从本页进入；验证事实以最新验收报告为准。历史数字保留日期和基线，计划只有明确实施后才算交付。

docs 默认忽略本地资料，仅放行索引、交接、设计与计划、本期及历史验收报告及模型摘要。标为“本地”的文件不会随普通克隆取得；公开说明在仓库 README 与 guides。不强制添加整个目录，不保存凭据或实际配置。
