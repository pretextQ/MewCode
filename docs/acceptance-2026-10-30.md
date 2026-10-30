# 2026-10-30 当前版本验收

基线：`4cef69f8dadda8595446fc871417bed558050286`，master，与验收时 GitHub master 一致；版本 0.3.0 / MIT。
环境：Windows、Python 3.14.4、uv 0.11.7、Docker Engine 29.8.2（Docker Desktop Linux engine）。

## 结论

**工程验收通过；独立审查的真实模型效果仅有限通过，继续默认关闭、仅提供意见。**

模型在三个完整正例上给出有效问题、一个完整负例上未误报；大 diff 部分覆盖时产生不成立的“函数缺失”结论。这条意见不能计为有效检出，覆盖标记也不能替代意见正确性复核。

未开展生产部署、远端修复 PR 创建或合并、当前版本真实模型完整告警回放、人工 TUI 交互验收。验收后的文档改动不包含在上述代码 SHA 中，另做链接与差异检查。

## 工程检查

| 检查 | 命令 | 结果 |
| --- | --- | --- |
| 全量离线套件 | `uv run --locked pytest -q -ra --timeout=120` | 1439 passed / 18 skipped，246.91 秒 |
| Ruff | `uv run --locked ruff check .` | 通过 |
| 全包类型 | `uv run --locked mypy mewcode` | 164 源文件通过 |
| Linux 目标类型 | `uv run --locked mypy --platform linux mewcode` | 164 源文件通过；不等于 Linux 本机执行 |
| 架构与规模 | `uv run --locked python scripts/engineering_guards.py` | 通过 |
| 锁文件 | `uv lock --check` | 93 包解析，检查通过 |
| 缺陷回植 | `uv run --locked python scripts/verify_restored_defects.py` | 四项当前实现 GREEN；旧行为 RED 数 2 / 5 / 5 / 1 |
| 子代理 | `uv run --locked python scripts/verify_subagent.py` | 86/86；无真实模型，失败任务日志是预期构造 |
| 依赖审计 | `uv run --locked pip-audit --local` | 无已知依赖漏洞；本包不在 PyPI，跳过本包审计 |
| 打包 | `uv build --no-sources` | sdist 与 wheel 成功 |
| CLI 入口 | `uv run --locked mewcode --help` | 正常输出；未启动交互会话 |

全量 18 个跳过项：15 个 Docker opt-in；2 个符号链接测试因本机权限/夹具条件不足；1 个 skill 用例因 parent 未配置 checker 跳过。15 个 Docker 项随后单独开启并全部通过，其余 3 个不计通过。两轮不合并成单次套件结果。

## Docker / Compose 真机专项

最初守护进程未运行。本次启动已安装的 Docker Desktop，确认 Linux engine 可用后执行：

```powershell
$env:MEWCODE_DOCKER_TESTS = '1'
uv run --locked pytest tests/test_sandbox_live.py -q -ra --timeout=1200
```

结果：**15 passed，111.80 秒，无跳过**。较长超时仅用于该专项的构建和依赖准备。

覆盖非 root、worktree 可写与根文件系统只读、挂载外宿主文件不可见、none 断网/bridge 出网、PID 限额、JSON 回读、容器清理、容器内日志/CI MCP、完整 Agent 与 MCP 往返、自起 Compose 依赖及容器内集成验证、依赖启动失败与取消清理、残留同名容器恢复。

模型及 Loki/GitHub 后端使用测试替身；容器、网络、进程、MCP 协议与 Compose 实际运行。结束后额外查询本次 live 命名空间容器及 Compose 网络，未发现残留。Docker Desktop 保持运行，镜像作为正常缓存保留。

## 远端 CI

2026-10-30 查询 [CI 40705152544](https://github.com/pretextQ/MewCode/actions/runs/40705152544)，head 为本报告完整 SHA，completed / success。Windows/Linux × Python 3.11/3.13 四个测试作业与 security regression 均 success。

该运行创建于 2026-10-28，今天仅核实结果，没有重新触发 CI，也不能声称它覆盖尚未提交的文档变更。

## deepseek-flash 真实模型审查

用户授权模型 deepseek-flash，费用上限 1 元。使用本地已有的官方 DeepSeek Anthropic 协议配置，不打印凭据、不改实际配置；仅发送合成案例，不发送本项目业务源码。

五个独立本地 Git 仓库固定 base/head，经真实 ReviewService、模型客户端与 SQLite 审计运行。没有工具、hook/MCP、脚本化响应、远端 push。每例最多一次调用，无 SDK 自动重试；输出上限 1024 token、user JSON 上限 12000 字节、超时 90 秒。五次预留全部按最高费率折算，事前计划上限约 0.722 元。

原样例之外补充明确的合成告警行为要求；没有向模型发送 expected_issue 或参考答案。场景、实际响应、固定 SHA、覆盖、用量及工件哈希见[模型摘要](acceptance-2026-10-30-model.json)。原始标签仍是初稿，本次逐条源码复核不替代维护者裁定。

| 样例 | 模型/服务结果 | 对照完整源码的复核 |
| --- | --- | --- |
| hidden_error | findings / completed，2 条 | 成立：明确要求无效输入抛 ValueError，新增 catch-all 却返回 None；另有吞 TypeError 的代码依据 |
| unbounded_retry | findings / completed，1 条 | 成立：持续失败时无限循环，无退避和退出边界 |
| removed_permission_check | findings / completed，1 条 | 成立：old 侧鉴权判断被删除，未授权可直接取得 secret |
| correct_arithmetic_fix | no_findings / completed | 合理：减号改加号符合明确的加法要求 |
| large_diff_unresolved_cause | findings / partial_coverage，1 条 | **不成立**：误称 add 不存在；完整候选仍有 `def add(a, b): return a - b`。根因未修，但意见理由错误，不计有效检出 |

大 diff 完整证据含 5000 行 filler 与末尾错误函数，预算截掉了模型输入后段。服务正确保存完整 diff、部分覆盖并验证意见行号；这些机制仍无法证明模型对未发送代码的推断可靠。

评分器只检查“是否出现 finding”，给出 4 个正例命中、1 个负例正确、precision/recall=1.0；这掩盖最后一个样例的错误理由，**不采纳为有效准确率**。本轮仅支持三个有效正例、一个正确负例和一个局部覆盖误判的结论。n=5、单次单模型、无候选 A/B，不能外推生产效果。

## 用量与预算

5 次调用：provider input 4993、output 2180、cache_read 512、cache_creation 0，共 7685 token；审查服务耗时合计 18.653 秒。

2026-10-30 查阅 [DeepSeek 官方人民币定价](https://api-docs.deepseek.com/zh-cn/quick_start/pricing/)：Flash 高峰每百万 token 缓存输入 0.04 元、未缓存输入 2 元、输出 8 元。为保守估计，将全部 7685 token 按最高的 8 元/百万折算，得 **0.06148 元（约 0.062 元）**。这不是账单金额，低于本轮 1 元授权上限。

没有使用模型搜索/抓取，没有扩大到修复回放。本次预算不自动延续到以后调用。

## 证据与复现边界

本地完整证据：`docs/evidence/acceptance-2026-10-30/`，保持 Git 忽略。含 pytest/docker/model 日志、一次性模型执行脚本、各例 full.diff、files.z、request.json、system.txt、response.txt、result.json 与汇总。模型脚本最初位于 `.pytest_cache/acceptance-2026-10-30/run_model.py`，归档副本仅作证据；复跑需对应目录结构、可用配置及新预算授权。

本报告与模型摘要受版本控制；模型工件哈希用于核对本地证据。没有归档实际配置、凭据、Git 数据库或 SQLite 数据库，也未上传原始日志。

## 文档收敛检查

统一总入口、演进索引与下次工作入口，更新本地定位、架构、M1/M2、测试环境及 backlog；为修复前报告和历史完成记录增加时效说明，保留原始执行证据。为本报告与模型摘要增加精确 Git 忽略例外，原始证据和其余本地资料继续忽略。

13 份当前说明的本地 Markdown 链接检查通过；20 个模型输入/响应/完整 diff/system 工件 SHA-256 与归档一致；用量合计、预算上限与被拒绝意见记录核对通过；git diff --check 通过。历史档案中的旧计划路径不按现行入口维护。

本轮只修改文档及两条忽略例外，没有改业务逻辑；提交与推送状态以 Git 记录为准。归档目录保留在本地，不会随普通克隆取得。

## 后续

1. 优先改进局部输入说明与意见语义复核，不能把有效行号当作结论正确，也不能从遗漏输入推断函数缺失。
2. 正式化样例契约与标签，增加真实案例、重复运行及基线/候选比较，再决定启用策略。
3. 当前版本真实模型修复到远端 PR 的完整回放、人工 TUI 冒烟与生产长期运行仍待验证。
4. 运行时监督未启动，不混入本次验收和文档整理范围。
