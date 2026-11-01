# 2026-11-01 intent anchor 首期设计与验收

起点 `4dd185d6e7790589133bc45755ec8a585828d65c`，master，开工工作区干净且实时 origin/master 一致。重新核对工作区及父目录，没有适用 AGENTS.md。用户授权继续开发、文档、提交推送；本轮选择 intent anchor 的保守首期，不调用付费模型、不创建真实远端修复 PR、不部署生产，不修改真实配置/凭据/业务数据库。

## 范围及设计决定

独立 Python detector `mewcode/supervision/intent_anchor.py`，复用默认关闭的监督开关、公共请求包装及既有总线预算。只用显式任务来源：TUI 用户原文（@ 内容扩展前）、headless/service 本次 task，以及 `supervision.steer(text)`。服务 task 是本 attempt 组装的告警/SOP/反馈 prompt，不另造语义目标；子代理按自己的显式 task 独立观察，不复制父锚点。公开 `run(conversation, intent="...")` 接受明确目标；旧调用没有 intent 时只跳过此 detector，不扫描历史或模型摘要。后台通知/无文本继续无来源；未启用监督的 skill fork 不因此启用。

启动时保存前 500 Unicode 字符，空白/缺省目标不触发。只做有界摘录，不是摘要或约束提取；长 prompt 后部的 SOP/限制/证据可能不在摘录。提醒标明来源和是否截断，转义标签，明确引用不能扩权或覆盖后续用户指令；不声称解决任意提示词注入。只读、无 Bash 和 plan mode 也可提醒目标，不要求无关验证、不改变工具门禁。

触发条件为总线累计至少 20 个真实决策响应轮且距最后用户输入至少 10 轮，首次最早在第 21 响应轮送达。按真实请求首响应计数，不要求流成功结束；工具调用/循环/首响应前重试/压缩或记忆请求不混作轮数。每次只在原额度允许的请求副本中注入，不增加请求或修改硬上限。20/10 门槛是本仓保守实现选择，未经模型效果校准。

成功发布后冷却 10 响应轮，每 run 最多接受三条。key 为 `builtin:intent-anchor:1` 至 `:3`，priority=40、TTL=2、reminder 通道，无 expect；沿用 128 key 容量、256 事件环和所有通道条数/字节限制，无豁免。容量拒收不花费次数，已接受但未选中、过期、supersede 仍花费次数/冷却。只统计真实请求首响应确认的 delivered；发布、选择或模型自述不叫送达，不核销 adopted/ignored，不证明遵守目标。

steer 替换明确目标、刷新静默计时，旧 pending/waiting 标 superseded；不重置本 run 次数和发布冷却。缺省/空白 steer 清除目标，不重放旧目标。旧在途请求可能物理送达，但不恢复旧目标。新公共 run、服务 attempt、失败/取消后的恢复重派清空来源/次数/计时，不做持久恢复。内部请求重试与上下文溢出恢复沿用本 run 明确来源和既有 pending/TTL，不重新武装 key 或增加请求。

完整 report / CLI JSON / 容器回读增加有界 `intent_anchor` 元数据与 `intent_anchor_suggested` 发布事件；元数据/事件不重复任务正文，完整 entries 中已发布提醒保留转义摘录。服务继续只保存原有紧凑总线摘要（含状态计数），不新增 detector 元数据、摘录或事件持久化。关闭时原输出和执行路径保持。

## 本轮验收

本地主机为 Windows，项目虚拟环境 Python 3.14.4；mypy win32/linux 是静态目标检查，不能冒称本机运行了 Linux 测试。Docker 使用真实 Linux 容器，远端 CI 另覆盖 Windows/Linux × Python 3.11/3.13。模型均使用脱网替身；行为验收不等于模型质量、防漂移或生产收益验证。原始日志保留在忽略的 `docs/evidence/intent-anchor-2026-11-01/`，不纳入实际配置或密钥。

| 检查 | 结果 |
| --- | --- |
| 监督与 TUI 专项 | 181 passed，20.70 秒；新增 intent anchor 35 项、TUI 来源 2 项 |
| 最终全量 pytest | 1704 passed / 19 skipped，259.92 秒；新增 37 项均纳入 |
| 真实独立 Docker | 16 passed，83.49 秒；Engine 29.8.2，无 mewcode 容器残留；不与全量数字相加 |
| Ruff / 全包 mypy | Ruff 通过；win32/linux 目标各 174 文件通过 |
| 工程守卫 / 四项回植 | 通过，未提高 ceiling；GREEN 均 0 失败，RED 为 2 / 5 / 5 / 1 个预期断言失败 |
| 锁文件 / 构建 / 组件 | uv lock --check、0.3.0 sdist/wheel 构建、verify_subagent 86/86 通过；构建包内新增源码与最终工作区字节一致，无新增依赖 |

覆盖实际交互/headless 请求、默认关闭、短任务与硬上限、20/10 门槛/冷却/三条上限、标签及 Unicode 截断、显式来源与历史/提醒隔离、旧 API 无来源、只读/plan/no Bash、steer 和无文本 steer、预算竞争/裁掉/TTL/容量拒收、首响应前重试/失败/取消、首响应后流失败、新 run/恢复、在途 steer、上下文溢出恢复、服务直跑两次 attempt 与 TUI 原文/通知边界。容器 probe 在真实容器内执行 20 次 ReadFile，检查第 21 响应轮的目标提醒和无 expect 送达状态；未调用真实模型。

首轮监督专项 174 passed / 22.80 秒。随后加入只读/plan 和 TUI 边界，扩展专项曾为 2 failed / 179 passed（21.28 秒）：测试错误地直接赋值只读 `Agent.plan_mode` 属性，改用现有 `set_permission_mode(PermissionMode.PLAN)` 接口后最终 181 passed。没有修改产品权限逻辑或放宽断言。另一次公开 Actions 查询的 PowerShell HTTP 请求发生意外 EOF，改用 Python 标准库只读访问公开 GitHub API 后成功；不涉及凭据或功能检查失败。

全量的 19 skipped：16 项真实 Docker 测试默认关闭；Windows 系统文件/符号链接条件不可用 2 项；父代理未配置 permission checker 的 skill executor 场景 1 项。独立启用 Docker 的 16 passed 不补算为全量通过数；Linux 的符号链接行为由本提交对应远端 CI 另行核实。工程规模仍为 Agent 1553/1575、app 2054/2100、新 detector 67/600 行，既有 ceiling 和锁文件均未修改。

可复现：`uv run pytest -q tests/test_intent_anchor.py tests/test_supervision.py tests/test_supervision_detectors.py tests/test_self_verify.py tests/test_tui_hardening.py --maxfail=10 --timeout=120`；全量 `uv run pytest -q -ra --maxfail=10 --timeout=120`；Docker 设置 `MEWCODE_DOCKER_TESTS=1` 单独运行 `tests/test_sandbox_live.py`。门禁为 `uv run ruff check .`、`uv run mypy mewcode --platform win32` / `--platform linux`、`uv run python scripts/engineering_guards.py --list`、`uv run --locked python scripts/verify_restored_defects.py`、`uv lock --check`、`uv build`、`uv run python scripts/verify_subagent.py`。

## Git 与边界

基线 [CI run 41293686354](https://github.com/pretextQ/MewCode/actions/runs/41293686354) 在本轮再次只读核实为 completed/success；它仅是已交付起点，不是新代码验收。本报告随新功能提交，所属 SHA 以 Git 历史为准；推送后另核对 origin/master 与该提交五项跨平台 CI，在交付答复报告实际状态，不将本地结果当作远端通过。

本期不扩展 token 预算预警、GitHub 评论/评审接入、observe、PR 发布持久化队列、多 Agent/TypeScript 迁移或自动合并。容器内实时 token 预算仍未接入，宿主结束后的汇总不能冒称实时能力。既有 self-verify 的“尝试/工具成功/覆盖 unknown”、dead-end 的 prepared 语义、权限/人工审查与人工合并边界保留。剩余路线须按后续新任务选定单项，不重复实施本期或历史已完成项。
