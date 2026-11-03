# 2026-11-03 文档整理记录

本次实际整理项目自有 Markdown 与 docs 资料，不实施产品功能，不读取真实配置或记忆、不启动真实用户 TUI、不调用真实模型。整理实施阶段仅改本地，随后用户明确授权提交推送。未发现适用 AGENTS.md；已读 MEWCODE.md 和指定入口，核对代码、测试、CI 与脚本引用。

## 起点与所有权

开工 master、origin/master 与实时远端 master 均为 `93e861a03328c6cc3d05a0bbfb272b44a9b281ac`，暂存区为空。唯一已有用户修改是 docs/README.md 的旧本地审查入口，该行原文和有效链接保留；未暂存、提交或丢弃。整理前对 76 份项目文档/运行时 Markdown 做路径和 SHA-256 清单，排除依赖、缓存、真实配置/记忆及原始生成证据内部的临时项目；每项写入前核对清单，发现新并行修改应保留。

## 结构与合并依据

- README.md 保留使用入口；MEWCODE.md 保留项目指令。guides 增加统一索引与开发维护流程，既有公开行为指南保留。
- 11 份阶段/基线报告和两份模型 JSON 集中到 docs/acceptance，保留原文件名便于追溯；JSON 内容字节不改，修复 Markdown 相对链接。
- 根目录 PROJECT_REVIEW、DOCKER_TEST_REPORT、REVIEW_FIXES 合并为 2026-10-15 历史档案，保留三种不同基线与原始数字、发现和提交，原文件删除。
- 删除 next-session-prompt：反复追加的会话授权、暂停、恢复与数字已由验收台账覆盖；当前安全约束迁入开发指南/待办，不保留可误触发旧开发的会话提示。
- 删除 fixes/README，导航并入历史索引；删除 fixes/review-2026-10-24-fixes，它与 guides/maintenance 的内容逐行核对，仅额外的过期验收引导不同，独有时效说明已由新入口覆盖。
- fixes/backlog 和 test-environment 合并重写到 docs/backlog 与 guides/development；纠正已经交付的监督、局部输入评估等旧待办，同时保留部署边界、凭据历史和验证限制。
- evolution/01-positioning 的定位及证据口径并入 guides/architecture；02～04 保留源码设计引用及独有模块/事务说明。演进索引的旧状态堆栈压缩，历史回放表移入 history/replay-2026-10-05。
- review 索引更新 A/B/V 当前状态和交付映射；原审查与 JSON 冻结于原基线。B5 一次性重发守卫尚未实现，不能与 N23 历史去重混为一项完成。

## 逐文件盘点与处置

下表覆盖整理前项目自有 Markdown；“原位保留”包含必要的导航或时效修正。运行时 agent/skill 文件属于产品资产，不作为可删说明文档。

| 原路径 | 处置 |
| --- | --- |
| `.mewcode/skills/frontend-design/SKILL.md` | 原位保留，运行时提示/技能资产；不改内容 |
| `.mewcode/skills/greeting-helper/SKILL.md` | 原位保留，运行时提示/技能资产；不改内容 |
| `.mewcode/skills/skill-creator/SKILL.md` | 原位保留，运行时提示/技能资产；不改内容 |
| `.mewcode/skills/skill-creator/agents/analyzer.md` | 原位保留，运行时提示/技能资产；不改内容 |
| `.mewcode/skills/skill-creator/agents/comparator.md` | 原位保留，运行时提示/技能资产；不改内容 |
| `.mewcode/skills/skill-creator/agents/grader.md` | 原位保留，运行时提示/技能资产；不改内容 |
| `.mewcode/skills/skill-creator/references/schemas.md` | 原位保留，运行时提示/技能资产；不改内容 |
| `DOCKER_TEST_REPORT.md` | 合并后删除，内容归 docs/history/review-2026-10-15.md#docker |
| `MEWCODE.md` | 原位保留；按需修复引用、时效或导航 |
| `PROJECT_REVIEW.md` | 合并后删除，内容归 docs/history/review-2026-10-15.md#review |
| `README.md` | 原位保留；按需修复引用、时效或导航 |
| `REVIEW_FIXES.md` | 合并后删除，内容归 docs/history/review-2026-10-15.md#fixes |
| `docs/README.md` | 原位保留；按需修复引用、时效或导航 |
| `docs/acceptance-2026-10-30.md` | 迁移到 docs/acceptance/acceptance-2026-10-30.md |
| `docs/evolution/01-positioning.md` | 合并后删除，内容归 guides/architecture.md |
| `docs/evolution/02-architecture.md` | 原位保留；按需修复引用、时效或导航 |
| `docs/evolution/03-m1-alert-driven.md` | 原位保留；按需修复引用、时效或导航 |
| `docs/evolution/04-m2-enterprise-capability.md` | 原位保留；按需修复引用、时效或导航 |
| `docs/evolution/05-pr-review-improvements.md` | 原位保留；按需修复引用、时效或导航 |
| `docs/evolution/06-tianshu-supervision-notes.md` | 原位保留；按需修复引用、时效或导航 |
| `docs/evolution/07-tianshu-test-notes.md` | 原位保留；按需修复引用、时效或导航 |
| `docs/evolution/08-next-stage-plan.md` | 原位保留；按需修复引用、时效或导航 |
| `docs/evolution/09-tui-redesign-plan.md` | 原位保留；按需修复引用、时效或导航 |
| `docs/evolution/10-review-a1-a5-fix-plan.md` | 原位保留；按需修复引用、时效或导航 |
| `docs/evolution/11-upstream-borrowing-plan.md` | 原位保留；按需修复引用、时效或导航 |
| `docs/evolution/README.md` | 原位保留；按需修复引用、时效或导航 |
| `docs/evolution/replay/after-rule-b.md` | 原位保留；按需修复引用、时效或导航 |
| `docs/evolution/replay/baseline-a.md` | 原位保留；按需修复引用、时效或导航 |
| `docs/fixes/README.md` | 合并后删除，内容归 docs/history/README.md |
| `docs/fixes/backlog.md` | 合并后删除，内容归 docs/backlog.md |
| `docs/fixes/review-2026-10-08-plan.md` | 原位保留；按需修复引用、时效或导航 |
| `docs/fixes/review-2026-10-24-fixes.md` | 合并后删除，内容归 guides/maintenance.md |
| `docs/fixes/review-2026-10-24-full.md` | 原位保留；按需修复引用、时效或导航 |
| `docs/fixes/test-environment.md` | 合并后删除，内容归 guides/development.md |
| `docs/history/README.md` | 原位保留，独有历史证据/索引 |
| `docs/history/evolution-2026-10-05.md` | 原位保留，独有历史证据/索引 |
| `docs/history/fixes-2026-10-04.md` | 原位保留，独有历史证据/索引 |
| `docs/history/review-2026-10-09-full.md` | 原位保留，独有历史证据/索引 |
| `docs/next-session-prompt.md` | 合并后删除，内容归 docs/README.md |
| `docs/next-stage-2026-10-30.md` | 迁移到 docs/acceptance/next-stage-2026-10-30.md |
| `docs/review/README.md` | 原位保留，修复前比较/复核；索引更新当前映射 |
| `docs/review/review-2026-11-01-upstream-parity-full.md` | 原位保留，修复前比较/复核；索引更新当前映射 |
| `docs/review/review-2026-11-02-current-borrowing.md` | 原位保留，修复前比较/复核；索引更新当前映射 |
| `docs/review/verification-2026-11-02.md` | 原位保留，修复前比较/复核；索引更新当前映射 |
| `docs/stage-2026-10-31.md` | 迁移到 docs/acceptance/stage-2026-10-31.md |
| `docs/stage-2026-11-01-dead-end.md` | 迁移到 docs/acceptance/stage-2026-11-01-dead-end.md |
| `docs/stage-2026-11-01-intent-anchor.md` | 迁移到 docs/acceptance/stage-2026-11-01-intent-anchor.md |
| `docs/stage-2026-11-01-self-verify.md` | 迁移到 docs/acceptance/stage-2026-11-01-self-verify.md |
| `docs/stage-2026-11-01-tui.md` | 迁移到 docs/acceptance/stage-2026-11-01-tui.md |
| `docs/stage-2026-11-01.md` | 迁移到 docs/acceptance/stage-2026-11-01.md |
| `docs/stage-2026-11-02-a1-a5.md` | 迁移到 docs/acceptance/stage-2026-11-02-a1-a5.md |
| `docs/stage-2026-11-02-borrowing.md` | 迁移到 docs/acceptance/stage-2026-11-02-borrowing.md |
| `docs/stage-2026-11-02-terminal.md` | 迁移到 docs/acceptance/stage-2026-11-02-terminal.md |
| `guides/agent-runtime.md` | 原位保留；按需修复引用、时效或导航 |
| `guides/architecture.md` | 原位保留；按需修复引用、时效或导航 |
| `guides/diagnostics.md` | 原位保留；按需修复引用、时效或导航 |
| `guides/hook-contract.md` | 原位保留；按需修复引用、时效或导航 |
| `guides/independent-review.md` | 原位保留；按需修复引用、时效或导航 |
| `guides/maintenance.md` | 原位保留；按需修复引用、时效或导航 |
| `guides/mcp-loading.md` | 原位保留；按需修复引用、时效或导航 |
| `guides/permission-semantics.md` | 原位保留；按需修复引用、时效或导航 |
| `guides/project-instructions.md` | 原位保留；按需修复引用、时效或导航 |
| `guides/service-reliability.md` | 原位保留；按需修复引用、时效或导航 |
| `guides/supervision.md` | 原位保留；按需修复引用、时效或导航 |
| `guides/tui.md` | 原位保留；按需修复引用、时效或导航 |
| `guides/windows-platform-notes.md` | 原位保留；按需修复引用、时效或导航 |
| `mewcode/agents/builtins/explore.md` | 原位保留，运行时提示/技能资产；不改内容 |
| `mewcode/agents/builtins/general-purpose.md` | 原位保留，运行时提示/技能资产；不改内容 |
| `mewcode/agents/builtins/plan.md` | 原位保留，运行时提示/技能资产；不改内容 |
| `mewcode/agents/builtins/verification.md` | 原位保留，运行时提示/技能资产；不改内容 |
| `mewcode/skills/builtins/backend_interview/SKILL.md` | 原位保留，运行时提示/技能资产；不改内容 |
| `mewcode/skills/builtins/commit/SKILL.md` | 原位保留，运行时提示/技能资产；不改内容 |
| `mewcode/skills/builtins/incident_triage/SKILL.md` | 原位保留，运行时提示/技能资产；不改内容 |
| `mewcode/skills/builtins/org_code_style/SKILL.md` | 原位保留，运行时提示/技能资产；不改内容 |
| `mewcode/skills/builtins/review/SKILL.md` | 原位保留，运行时提示/技能资产；不改内容 |
| `mewcode/skills/builtins/test/SKILL.md` | 原位保留，运行时提示/技能资产；不改内容 |

## 非 Markdown 核对

两份受版本控制的模型摘要随验收迁移并检查 SHA-256/JSON 可读性；replay 两份 JSON 继续留在原目录，被历史回放引用且 m3_replay.py 仍以该目录为默认输出。review 中原 findings JSON、observations、复现脚本与日志保留原基线语义。

盘点时 docs/evidence 内有 728 份非 Markdown 资料（排除嵌套 .git 与 __pycache__），review 内 51 份，另有两份 replay JSON；原始证据按实验目录和报告关联，不以“没有单文件链接”判为废弃。SVG/PNG 对应视觉过程，raw 对应终端流，JSON/日志/脚本支持失败、模型与交付记录，数据库、冻结快照、二进制或临时环境未经核验不删。详见[证据索引](../evidence/README.md)。

## 路径依赖与分发

检查 .github、scripts、tests、mewcode 与打包配置。README 仍供包元数据使用；内置 agents/skills 和项目技能原位不变。源码引用的 evolution/02～04 原位保留并放行版本控制；sandbox.py 仅改文档注释中的 backlog 路径。m3_replay.py 的默认 replay 输出、终端捕获脚本的 evidence 输出不改。

.gitignore 删除已迁移入口的过期例外，放行新的索引、验收、待办及选定历史文档；不放行原始 evidence、配置、旧本地档案或临时环境。部分原始历史/审查链接明确标注本地，普通克隆不包含这些资料，不能作为唯一公开维护入口。

## 保留的未解决事实

U1 覆盖真实配置且没有可靠备份、仍未恢复；Linux tree-service timeout/cancel 偶发失败根因未确认；Windows/Linux GUI 人工验收未完成；临时目录/卷未经核验继续留存。候选能力没有因本次整理获得实施授权。具体边界与证据集中在[维护待办](../backlog.md)。

## 本次验证

- 静态检查覆盖 68 份 Markdown（58 份说明文档及 10 份内置运行时 Markdown；另外 7 份项目技能逐字节核对），427 个本地链接无缺失目标、无失效 Markdown 锚点；代码示例中的虚拟链接不计入。58 份说明文档均能从项目 README 沿导航到达，没有孤立文档。
- 两份模型 JSON 可解析，SHA-256/字节与原路径按 Git checkout 行尾过滤后的内容一致；原始工作区字节原样迁移。17 份 Agent/Skill Markdown 字节不变，用户原有本地审查入口原文保留。
- 版本控制白名单、原始 evidence/本地历史忽略边界检查通过。原始证据和本地档案没有被批量放行；15 个仅本地存在的链接目标均属于索引明确标注的本地资料。
- `scripts/engineering_guards.py` 通过；`ruff check mewcode/service/sandbox.py` 通过；`git diff --check` 与暂存区检查通过。sandbox.py 去除文档字符串后的 AST 与 HEAD 相同，唯一源码差异是注释中的文档路径。
- 没有新增产品测试或运行全量 pytest、真实模型、用户 TUI、Docker/服务验收；本次不涉及产品行为。历史测试和 CI 不作为本次重新运行的结果。首次 JSON 比较发现 Git blob 为 LF、工作区为 CRLF；按仓库 checkout 过滤后字节一致，未改写数据来消除差异。
- 整理完成、提交前复核：master、origin/master 与实时远端 master 均为 `93e861a03328c6cc3d05a0bbfb272b44a9b281ac`，当时暂存区为空。后续按用户新授权提交推送；本次提交及对应 CI 以实际 Git/Actions 与交付答复为准，避免为记录自身 SHA 无限追加提交。用户原有审查入口及其本地说明单独留工作区，不纳入提交。修改范围为文档、忽略例外和一行源码文档引用，配置、记忆、运行时提示、临时目录和卷均未触碰。
