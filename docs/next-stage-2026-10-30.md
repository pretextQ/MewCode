# 2026-10-30 下一阶段实施验收

代码起点：`a85444ccddc359e302b21db7f56896f810c637c5`。原验收报告及 SHA、原模型响应不改写。Windows / Python 3.14.4 / uv 0.11.7；Docker Engine 29.8.2 重新确认可用。

## A：审查范围与评估

已实现：每次请求 versions 中包含服务生成的逐文件 full/partial/none、原因、diff/sent UTF-8 字节数和 diff-only 范围声明；hunk 标题界定实际上下文。完整 diff 不截断。覆盖账本随最终补丁裁剪重新生成，和文件名、JSON 转义一起计入精确 user 输入上限。无法容纳事实与有效补丁时不请求模型。

模型指令明确：完整 diff 不等于完整文件，省略区域不证明函数不存在；无源码依据则在 summary 中保留未知。服务仍只验证格式、版本和实际输入的增删行，不能证明语义正确。功能默认关闭、仅提供意见；无工具、多轮检索、分块或自动返工。

原始五例 fixture 保持不变；新增 `independent_review_cases_v2.json` 明确 ValueError、持续失败传播、鉴权、加法及大 diff 的行为契约和复核边界。评估器保留存在性数字，另报 valid/invalid/unreviewed finding、有效正例和未复核/未覆盖案例。复核必须给源码证据；原“add 不存在”即便存在性命中也为无效。

## 真实模型比较

[完整摘要](next-stage-2026-10-30-model.json)。每组五次 deepseek-flash 请求，单例最多一次、无自动重试、user 输入 12000 字节、输出 1024 token、90 秒。基线代码从 a85444c 导出到隔离临时副本，候选使用当前改动；两组使用相同 v2 源码、契约及配置。逐例 full.diff SHA-256 相同，合成提交 SHA 分别记录。候选因覆盖事实及指令占用预算，实际补丁输入更短。

| 组别 | 有效正例 | 完整负例无误报 | 未完成大 diff | 有效/无效 finding |
| --- | --- | --- | --- | --- |
| 同配置重跑基线 | 3 | 1 | 1（输出上限） | 4 / 1 |
| 候选 | 3 | 1 | 1（输出上限） | 3 / 1 |

逐条对照完整源码：隐藏 ValueError、无限重试、删除鉴权均有成立意见；两组的额外重试意见均含过度断言，分别称循环不可杀死、泛称捕获所有异常及 cancellation，按整条意见严格拒绝。Exception 不捕获 BaseException/asyncio.CancelledError。正确修复确实从减法改成加法；大 diff 仍在 filler 后定义减法 add，两组均未完成响应，不能算正确负例或有效检出。没有用脚本化响应或模型自评分作质量证据。

**未证明效果改善。** 每组 n=5、单次调用；旧历史误判已被评分回归拒绝，但提示词不能保证模型不再误判。保持默认关闭。

2026-10-30 核查 [官方人民币价格](https://api-docs.deepseek.com/zh-cn/quick_start/pricing/)：Flash 最高输入 2 元/百万、输出 8 元/百万。每次按所有预留 token × 8 元/百万预检；未知用量停止。候选五次已知用量后释放未用预留，再为隔离基线五次预留 0.72192 元（加候选实际保守估计仍低于新增 0.90 元）。新增十次保守估计 **0.136408 元**，连同历史 0.06148 元累计 **0.197888 元**，不是账单金额。没有后续模型调用。

原始请求、响应、full.diff、固定 SHA、用量、耗时、哈希与执行脚本在本地 `docs/evidence/next-stage-2026-10-30/`，保持忽略；未归档实际配置或凭据。摘要版本化；历史工件不修改。

## A 工程验证

- `uv run --locked pytest tests/test_independent_review.py -q --timeout=120`：54 passed。
- Ruff、Windows/Linux 目标全包 mypy（各 164 文件）、工程守卫、锁文件通过。
- 四项缺陷回植：当前 GREEN；旧行为分别触发 2 / 5 / 5 / 1 个断言失败。
- `uv run --locked pytest -q -ra --timeout=120`：1441 passed / 18 skipped，226.62 秒。15 个 Docker opt-in、2 个本机符号链接权限、1 个 parent checker 缺省跳过，不计通过。A 未触及容器接线，Docker 专项在 B 配置接线后补跑。

## B：最小监督内核

已实现，默认关闭，纯 Python，无默认 detector、提醒或自动外部动作。[监督指南](../guides/supervision.md)与 `.mewcode/config.example.yaml` 给出开关和精确语义。

每个 Agent 持有隔离总线；公共循环调用对应 run，模型决策请求首响应计 turn，provider 重试有独立 request ID。支持跨通道 key 去重、优先级/分类、TTL、逐通道数量及渲染 UTF-8 字节预算、容量限制和有界事件环。system/reminder/status 默认预算分别为 2/2048、3/4096、8/2048，没有分类豁免；状态只供 report，不发模型。

提醒在压缩/工具结果裁剪之后进入本次请求副本，不进入持久历史或 fork。选中/渲染不是送达，首响应确认后才记 delivered；首响应前失败/取消不确认、不给 TTL 加轮，首响应后失败保留已送达。无 expect 只计送达；tool_call 与实际 tool_success 分开，完整窗口内匹配为 adopted，到期未匹配才 ignored；失败、取消或结束未耗尽窗口仍 waiting。预算裁掉、未注入、状态项不 ignored。

新 run/恢复/服务新 attempt 重置，用户 steer 将旧 pending/waiting supersede；在途已发送请求仍如实记送达，不恢复旧 expect。子代理、teammate、worktree 子代理继承 enabled，账本及历史提醒隔离。交互/headless、服务直跑/容器最小配置已接线；CLI JSON 提供完整 report，服务持久化有界 JSON 摘要。现有 hooks 队列和权限边界保留。

A 评分器另补一条边界回归：显式 unreviewed 即使已填满裁定列表，也必须仍是未复核案例。没有新增模型调用。

## B 工程验证与过程发现

- 新增监督专项：33 passed；覆盖两条真实 Agent 循环、默认关闭、实际请求文本、通道预算、去重、TTL、无 expect、失败工具与调用尝试、首响应前重试、流失败、取消、上下文恢复、新 run、在途 steer、实际子代理创建、服务直跑、容器开关传递和 SQLite 审计。
- 既有 Agent/服务/沙箱配置/子代理专项：229 passed；最终审查/沙箱/监督专项 131 passed（含审查 55 例）。TUI/监督修复定向：36 passed。
- Docker 真机重跑：**16 passed，72.17 秒**。在既有完整 CLI/MCP 路径验证 enable_supervision、两轮真实请求及空总线；另有显式测试探针在真实容器内运行 Agent/Bash（exit 1），证明“尝试已采纳、工具成功未采纳”、状态通道不注入及请求副本不重复发送。模型与后端为替身；不作为模型效果证据。
- Docker 首跑 1 failed / 15 passed（75.67 秒）：新增 CLI 摘要断言误放入后续 stale-container 测试；移回正确用例后重跑通过，原失败日志保留。
- 全量首跑 2 failed / 1472 passed / 19 skipped（250.44 秒）：TUI 接线对旧 duck-typed Agent 替身直接访问 supervision；改为仅在总线存在且启用时调用后，36 例定向通过，最终全量重跑通过。
- Ruff、Windows/Linux 目标全包 mypy（各 167 文件）、工程守卫、锁文件、四项回植通过。新增三份纯 Python 源文件，无依赖变更，无规模 ceiling 提升。

最终全量 `uv run --locked pytest -q -ra --timeout=120`：**1475 passed / 19 skipped，232.62 秒**。跳过为 16 Docker opt-in、2 符号链接权限、1 parent checker 缺省；Docker 另跑通过，不将两轮相加冒充单次套件。子代理 86/86；`uv build --no-sources` 的 sdist/wheel 成功，wheel 确认包含三份监督源码。12 份当前文档本地链接、40 个新模型工件哈希及候选源码哈希核对通过。原始日志见本地忽略的 `docs/evidence/next-stage-2026-10-30/`；首跑失败不覆盖。首期没有四类 detector，监督基础接线验收不代表监督效果改善。

## 提交与远端

A：[`c653b77`](https://github.com/pretextQ/MewCode/commit/c653b7773ec980c4982213570f44757d03bcf067)，已推送 origin/master。[CI 40998116499](https://github.com/pretextQ/MewCode/actions/runs/40998116499) Windows/Linux × Python 3.11/3.13 与 security regression 五个作业 success。

B：[`37b74ef`](https://github.com/pretextQ/MewCode/commit/37b74efe2f470a5a3b443cb8875da842f67d057a)，已推送 origin/master，远端 SHA 与本地一致。[CI 40999894769](https://github.com/pretextQ/MewCode/actions/runs/40999894769) Windows/Linux × Python 3.11/3.13 与 security regression 五作业均 success，没有新增远端失败。

最后仅收敛文档、记录提交和已完成的代码 CI；没有再改运行逻辑或调用模型，不因纯文档重复本机全量。文档收尾提交的远端状态以最终 Git/CI 检查为准。

旧验收 20 个模型工件哈希复核一致，原模型摘要和 v1 fixture 与 a85444c 无差异。Docker live 命名空间容器无残留；其他项目已有网络未处理。
