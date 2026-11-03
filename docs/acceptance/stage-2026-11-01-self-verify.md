# 2026-11-01 self-verify 首期设计与验收

起点 `cc98162392f02b352dba61133c1a233e654bbb27`，master，开工工作区干净且实时远端一致。工作区及父目录没有适用 AGENTS.md。用户授权实现、文档、提交推送；不调用付费模型、不创建远端修复 PR、不部署生产。

## 实施前决定

依据本仓现有共享 Agent 循环独立实现 Python 观察器。只追踪实际通过权限、hooks、参数验证并成功返回的内置 EditFile/WriteFile 对 `.py`/`.pyi` 的编辑；不是任务意图或完成声明分类器，不扫描现存 dirty worktree。只读、问答、纯文档和其他语言编辑不触发。无内置可用 Bash 或 plan mode 不投递建议；无适用测试的 Python 任务由建议文案要求如实解释替代证据和限制，不强制跑测试。

在编辑后的下一次原预算允许的决策请求选择提醒前检测；不追加收尾请求，每 run 最多一条、TTL=2、priority=60，服从原总线去重/容量/字节预算。实际执行的受支持验证命令即计尝试（失败亦然），后续编辑形成新 revision；尝试、tool success、覆盖充分严格分开。覆盖始终 unknown，不能自动推断目标、cwd 或覆盖文件；一次尝试抑制当前 revision 的催促不是解除质量义务。已提醒后再次编辑仍记录差距但本 run 不重复催促。

新 expect 事件 `verification_attempt/Bash` 只由实际执行且识别为验证的内置 Bash 发出；普通 Bash、模型自述或参数被拒绝不匹配。已组装、选择未确认不算送达。steer 使旧义务暂停且旧提醒 superseded，后续新编辑才恢复观察；在途旧工具结果不恢复旧义务。公共 run、服务新 attempt、取消或重启后的新 run 均重置，不跨进程恢复观察状态，已有物理改动不代表自动重建义务。

## 实现与可观察边界

新增 `mewcode/supervision/self_verify.py`，在实际工具执行与最终请求副本选择处接线；原 Agent 三条工具执行路径共用观察包装，关闭时直接执行原工具。总线增加 `verification_attempt` expect 和有界 self_verify 摘要，沿用既有送达确认与事件环，不改变 dead-end 的 prepared 语义。没有新增依赖、配置字段、状态机、容器协议或 ceiling。

命令识别仅支持直接 pytest/mypy/ruff check、python -m 形式，以及不带包装选项的字面 uv run；帮助、版本、收集/列举、shell 组合/变量和 ruff 自动修复回退 unknown。详情见[监督指南](../../guides/supervision.md)。验证开始关联当时的编辑 revision，失败仍算尝试，结果只记录工具 is_error；不解析测试结果、不验证 cwd/目标范围，覆盖一直 unknown。一次无关目标的测试也可能抑制当前催促，不能将此当作质量门禁或“充分验证”。

只读/问答/纯文档及其他语言编辑不触发；Python 无适用检查时由建议要求解释替代证据和限制，不强制测试。Bash、自定义工具、子代理的编辑，以及旧 dirty worktree、失败写入后的部分改动不盘点。相同内容的成功写入也可能触发；最后一轮才编辑不会追加请求。每 run 一次建议意味着提醒后再次编辑只记录差距，不再次催促。steer 暂停旧义务，旧在途工具结果不重建它；新编辑恢复观察但已使用 key 不重武装。服务保留现有紧凑落库摘要，不持久化完整 detector 事件或 revision；公共报告和 CLI/容器回读可查看完整有界报告。

## 验证

Windows 11，Python 3.14.4；mypy 仍按 Python 3.11 类型目标分别检查 win32/linux。所有 LLM 使用替身；真实 pytest 子进程只运行临时合成仓库。真实 Docker 使用假模型与假配置；无真实远端修复 PR、付费模型调用或生产部署。

| 检查 | 结果 |
| --- | --- |
| self-verify 与现有监督专项 | 最终 141 passed，19.27 秒；其中新增首期 88 项 |
| 最终全量 pytest | 1667 passed / 19 skipped，250.78 秒；88 项新增用例全部纳入 |
| 独立真实 Docker | 16 passed，72.41 秒；最终源码的共享接线再单独 1 passed / 11.61 秒；Engine 29.8.2，无 mewcode 容器残留；各轮单独计数，不与全量相加 |
| Ruff / 全包 mypy | 通过；win32/linux 各 173 文件 |
| 工程守卫 / 四项缺陷回植 | 通过，未提高 ceiling；GREEN 各 0 失败，RED 为 2 / 5 / 5 / 1 个预期断言失败 |
| 锁文件 / 构建 / 组件验证 | uv lock --check、0.3.0 sdist/wheel、verify_subagent 86/86 通过；无新增依赖 |

覆盖默认关闭、只读/纯文档/其他语言豁免、无适用测试的条件建议、成功与失败的真实 pytest、先验证再编辑、同轮编辑后验证、后续新编辑、内置 EditFile/WriteFile、三条执行路径、普通/未知/同名替换工具、权限/hooks 拒绝、无验证工具/plan mode、硬上限、预算竞争/裁掉/TTL/容量、请求重试、首响应前取消/失败、首响应后流失败、新 run、在途 steer 及在途工具结果、服务直跑开关与容器内共享接线。检查实际请求、实际文件、revision/结果/送达与核销状态，不凭提醒已组装或模型自述声称通过。

初轮专项 6 failed / 108 passed：新增事件字段 `kind` 与 `_event` 参数重名，造成验证工具执行前异常；mypy 同时定位了这两处，修正为 verification_kind 后 114 passed。首次 Ruff 发现长行，修正后通过。扩展门禁测试先因 RuleEngine 参数名错误出现 4 failed / 123 passed；改用 append_local_rule 后仍因未设置临时规则文件导致同样 4 failed / 123 passed，补足临时路径后 135 passed。测试补充阶段出现一次 1 failed / 140 passed：同一临时目录的二次写入被既有 read-before-write 门禁正确拒绝，改用新文件后最终 141 passed。另一次 Ruff 的测试导入排序已修正；没有放宽真实门禁。

首次 Docker 检查 16 skipped，因为 Desktop daemon 未运行；启动本机 Docker Desktop 后，真实专项最终 16 passed，容器中实际验证失败仍正确核销为 attempted、successful_revision=0、coverage=unknown。默认全量不启用 Docker。最终六项边界测试补充前的第一轮全量为 1661 passed / 19 skipped，261.10 秒；不替代上表最终全量。原始日志保留在忽略的 `docs/evidence/self-verify-2026-11-01/`；不纳入凭据、真实配置或业务数据。

最终全量的 19 skipped：16 项为独立启用的真实 Docker 测试，2 项为本机 Windows 系统文件/符号链接权限不可用，1 项为测试设置中父代理未配置 permission checker 的 skill executor 场景。独立 Docker 通过不补算这轮全量数字；Linux 符号链接行为由对应远端 CI 另行验证。最后构建已检查 wheel/sdist 均包含最终 self_verify 源码。

可复现命令：`uv run pytest -q tests/test_self_verify.py tests/test_supervision.py tests/test_supervision_detectors.py --maxfail=10 --timeout=120`；全量 `uv run pytest -q --maxfail=10 --timeout=120`（最终补充 -ra 展示跳过原因）；独立 Docker 设置 `MEWCODE_DOCKER_TESTS=1` 后运行 `tests/test_sandbox_live.py`。门禁为 `uv run ruff check .`、`uv run mypy mewcode --platform win32` / `--platform linux`、`uv run python scripts/engineering_guards.py`、`uv run --locked python scripts/verify_restored_defects.py`、`uv lock --check`、`uv build`、`uv run python scripts/verify_subagent.py`。

## Git 与效果边界

开工基线 `cc98162` 的远端 [CI run 41273481659](https://github.com/pretextQ/MewCode/actions/runs/41273481659) 已于本轮另行核实，Windows/Linux × Python 3.11/3.13 和 security 五项全部 success。它不是本次功能验收。本报告随功能提交交付；所属 SHA 以 Git 历史为准。推送后另行核对 origin/master 与本提交五项 CI，在交付答复报告实际状态；本地通过不混作远端通过。

本轮只验证行为接线，无模型质量、效率或生产收益结论。不实现 token 预警、intent anchor、GitHub 评论/评审接入、observe、PR 发布持久化队列、多 Agent/TypeScript 迁移或自动合并；现有权限、人审 PR 与人工合并边界继续有效。
