# 2026-10-24 审查修复与维护交接

原始审查基线：`03f6687`。代码修复验证基线：`6d457b0`。本轮覆盖 N01–N41，修复使用独立提交，并逐次推送到 `origin/master`。
当前版本为 0.3.0，许可证为用户选定的 MIT。实际代码版本以 `git rev-parse HEAD` 为准。

## 验证

- 本地全量 pytest：1363 passed / 18 skipped；真实环境跳过项不计为通过。
- [代码 CI](https://github.com/pretextQ/MewCode/actions/runs/40108096544)：Windows/Linux × Python 3.11/3.13 四组测试及安全检查全部通过，验证代码提交 `6d457b0`。
- Ruff 全仓检查通过。
- `mypy mewcode`：159 个源文件通过；Windows 与 Linux 目标平台均通过。CI 已覆盖全包。
- `uv lock --check` 通过；0.3.0 sdist 和 wheel 构建通过。
- `uv run pip-audit --local`：未发现已知依赖漏洞；本项目自身不在 PyPI，工具将其列为跳过项。
- `uv run python scripts/verify_subagent.py`：86/86 检查通过，使用临时 fixture，不读取真实配置。
- `scripts/mewtest.sh` 通过 Bash 语法检查；真实 tmux 交互未执行。
- Docker CLI 存在但守护进程不可用，真实容器隔离测试未执行；普通测试中的容器替身验证不能替代它。
- 未调用真实模型或向 demo 仓库创建 PR。

## 提交映射

N07 的 YAML 风险采用限制输入大小、深度、事件数并拒绝别名处理；原报告的 GB 级内存放大表述不作为已复现实测。
N10 分页遗漏既可能造成等待，也可能错误放行 CI，已增加后页失败项回归。
N41 的目录改名暴露出 56 条被早期扫描失败遮挡的类型错误，已修复，skill 的公开名称保留连字符。
目录改名随 N40 的脚本验证提交落地，N41 后续提交完成类型修复和全包 CI 门禁。

| 编号 | 审查项 | 提交 |
| --- | --- | --- |
| N01 | 伪取消恢复重跑必撞 worktree 已存在，确定性 escalate | [2a0ae2e](https://github.com/pretextQ/MewCode/commit/2a0ae2e), [db06661](https://github.com/pretextQ/MewCode/commit/db06661) |
| N02 | Bash 白名单不防 `%VAR%` 展开与 UNC，沙箱外文件零确认读取 | [bb148df](https://github.com/pretextQ/MewCode/commit/bb148df), [2e0d1af](https://github.com/pretextQ/MewCode/commit/2e0d1af) |
| N03 | `run_to_completion` 丢弃 thinking_blocks，thinking 子代理第二轮必 400 | [a3a171f](https://github.com/pretextQ/MewCode/commit/a3a171f) |
| N04 | 危险黑名单可被 flag 顺序/长选项绕过 | [2640353](https://github.com/pretextQ/MewCode/commit/2640353) |
| N05 | ALLOW_ALWAYS 的 `前缀*` 规则放行链式命令 | [bfbb654](https://github.com/pretextQ/MewCode/commit/bfbb654) |
| N06 | MCP 工具不受 `disallowed_tools` 约束 | [7301929](https://github.com/pretextQ/MewCode/commit/7301929) |
| N07 | Compose YAML 别名炸弹 + 依赖容器无资源限额 | [13e112f](https://github.com/pretextQ/MewCode/commit/13e112f) |
| N08 | 内置 org-code-style 可被仓库同名 skill 覆盖，SELF-CHECK 证据可被替换 | [2ca10bb](https://github.com/pretextQ/MewCode/commit/2ca10bb), [8800295](https://github.com/pretextQ/MewCode/commit/8800295) |
| N09 | CI 无 pip-audit/dependabot、workflow 权限过宽、矩阵不钉 Python 版本 | [8bff92e](https://github.com/pretextQ/MewCode/commit/8bff92e) |
| N10 | CI 门禁无重试/退避，限流误判，check-runs 不分页；瞬态故障放大为 escalate | [db0e5c6](https://github.com/pretextQ/MewCode/commit/db0e5c6) |
| N11 | human_review 无写回入口：merged 指标恒 0，去重窗口过期重复开 PR | [c641bb9](https://github.com/pretextQ/MewCode/commit/c641bb9) |
| N12 | worker 消费者可被一次 JobStore 异常永久杀死，服务静默停摆 | [2f8e34b](https://github.com/pretextQ/MewCode/commit/2f8e34b) |
| N13 | 主循环不处理 ContextOverflowError，压缩熔断后每发必溢出 | [82b04e7](https://github.com/pretextQ/MewCode/commit/82b04e7) |
| N14 | auto_memory 依赖模型回显全文再整体覆盖，漏节即丢记忆，写入非原子 | [ed9f710](https://github.com/pretextQ/MewCode/commit/ed9f710) |
| N15 | SessionMeta 非原子写，损坏后 jsonl 完好也无法 resume | [eb3e0da](https://github.com/pretextQ/MewCode/commit/eb3e0da) |
| N16 | 沙箱探测负结果永久缓存，daemon 晚启动即全生命周期禁沙箱 | [1d44574](https://github.com/pretextQ/MewCode/commit/1d44574) |
| N17 | 优雅关停先停 runtime 后关 HTTP，drain 期间产生"202 但不入队"的 job | [72d5a67](https://github.com/pretextQ/MewCode/commit/72d5a67), [7eff6b9](https://github.com/pretextQ/MewCode/commit/7eff6b9), [6b424be](https://github.com/pretextQ/MewCode/commit/6b424be) |
| N18 | 长期运行资源只增不减：worktree、`mewfix/*` 分支、旧镜像均无周期清理 | [db525ef](https://github.com/pretextQ/MewCode/commit/db525ef) |
| N19 | 默认 `ci_timeout_seconds == job_timeout_seconds`，CI 超时分支不可达 | [239753f](https://github.com/pretextQ/MewCode/commit/239753f) |
| N20 | 失败 attempt 的 token 用量不入账，预算与 `/costs` 低估 | [2ed166b](https://github.com/pretextQ/MewCode/commit/2ed166b) |
| N21 | 父/子代理共享 tool-results 目录，任一方 compact 删另一方引用的落盘文件 | [bda9a66](https://github.com/pretextQ/MewCode/commit/bda9a66) |
| N22 | Bash/ReadFile 无输入输出大小上限，runaway 命令先撑爆内存再谈截断 | [685e5ad](https://github.com/pretextQ/MewCode/commit/685e5ad) |
| N23 | system reminder 每迭代累积并持久化重放；副对话 prompt 双发 | [294f7e5](https://github.com/pretextQ/MewCode/commit/294f7e5) |
| N24 | 流式渲染每增量重建全量 RichText，O(n²) 开销 | [a1a1499](https://github.com/pretextQ/MewCode/commit/a1a1499) |
| N25 | 权限 YAML 每次评估重读盘解析，热路径每调用最多 6 次文件 IO | [0e1bf52](https://github.com/pretextQ/MewCode/commit/0e1bf52) |
| N26 | fnmatch 平台敏感，deny 规则跨平台语义不一致 | [075de33](https://github.com/pretextQ/MewCode/commit/075de33) |
| N27 | Bash/Grep 固定 utf-8 解码，中文 Windows 输出 GBK 乱码 | [73f75fd](https://github.com/pretextQ/MewCode/commit/73f75fd), [6d457b0](https://github.com/pretextQ/MewCode/commit/6d457b0) |
| N28 | FileCache/FileStateCache 无上限，长会话只增不减 | [c0b3b05](https://github.com/pretextQ/MewCode/commit/c0b3b05) |
| N29 | MCP 名称冲突静默覆盖，`mcp_{server}_{tool}` 拼接有歧义 | [91631bf](https://github.com/pretextQ/MewCode/commit/91631bf) |
| N30 | agent loader 启动后不发现新文件，文件型 agent 每次 get 重解析 | [c07898b](https://github.com/pretextQ/MewCode/commit/c07898b) |
| N31 | worktree 子代理失败路径不清理 worktree，泄漏磁盘与分支 | [ed5e42d](https://github.com/pretextQ/MewCode/commit/ed5e42d) |
| N32 | plugin source 注册是 pass；hook 的 agent 执行器 stub 返回 success=True | [c86cafd](https://github.com/pretextQ/MewCode/commit/c86cafd) |
| N33 | EditFile 混合行尾时全量统一行尾，与"未触碰字节不变"承诺不符 | [cfade7c](https://github.com/pretextQ/MewCode/commit/cfade7c) |
| N34 | 子代理被中断时 trace 节点永远停在 running | [ce33459](https://github.com/pretextQ/MewCode/commit/ce33459) |
| N35 | README 回放命令引用不存在文件；测试数/脚本清单/结构树过时 | [1981639](https://github.com/pretextQ/MewCode/commit/1981639) |
| N36 | 版本 0.2.0 从未 bump；description 不符；缺 LICENSE 与打包元数据 | [68050dc](https://github.com/pretextQ/MewCode/commit/68050dc), [193268a](https://github.com/pretextQ/MewCode/commit/193268a) |
| N37 | 依赖下限与实锁版本跨大版本脱节 | [8888ea0](https://github.com/pretextQ/MewCode/commit/8888ea0) |
| N38 | docs 时效：next-session-prompt 停在 10-09（本记录已同步索引与 backlog） | 本提交 |
| N39 | .gitignore 未显式列工具缓存目录，靠工具自写 ignore 兜底 | [5adfc01](https://github.com/pretextQ/MewCode/commit/5adfc01) |
| N40 | verify_subagent.py 依赖 gitignored 本机状态；mewtest.sh 启动路径指向包目录 | [5b8acf6](https://github.com/pretextQ/MewCode/commit/5b8acf6) |
| N41 | `backend-interview` 目录名含连字符，全包 mypy 被阻 | [f5fe67f](https://github.com/pretextQ/MewCode/commit/f5fe67f) |

## 后续维护

开始下一项工作时先检查工作区和当前分支，不要重复启动本轮已完成的修复。
本轮新事实与修复结果以本文为受版本控制的交接入口；本地 `docs` 中的原始报告保留审查时的证据。

常规检查：

```bash
uv run ruff check .
uv run mypy mewcode
uv run pytest --timeout=120
uv lock --check
uv run pip-audit --local
```

## 第一阶段最小工程守卫（2026-10-28）

基线为 `1e8911e`，本阶段不重新修复 R/N 历史问题，不接入模型审查或监督闭环。
可复现检查（无真实模型、无远端 PR 操作）：

```bash
uv run --locked python scripts/engineering_guards.py
uv run --locked python scripts/engineering_guards.py --list
uv run --locked python scripts/verify_restored_defects.py
uv run --locked ruff check .
uv run --locked mypy mewcode
uv run --locked pytest -q --timeout=120
uv lock --check
```

### 架构边界

已核对当前依赖：service 不依赖 TUI，permissions 不依赖 service，tools/impl 不依赖 agent。
工具实现使用 `tools.base` 协议；本阶段禁止其直接导入整个 `mewcode.agent` 模块，
以免引入 Agent 状态耦合，不限制已有 `tools/agent_tool.py` 的协调职责。
TUI 边界包括 Textual、app、driver、各 dialog、teammate_tree 和 commands/completion。

`scripts/engineering_guards.py` 用标准库 AST 检查这三个目录中的 Python 文件，
包括函数内、TYPE_CHECKING 内的静态 import、绝对/相对 import 和包 __init__；
文件名统一为 POSIX 路径，诊断包含文件及行号。
字符串动态导入、运行时属性访问、重新导出及传递依赖不在本阶段覆盖范围，
这不是任意数据流或私有属性访问分析器。新增 TUI 模块须同步评审边界清单。
`tests/test_engineering_guards.py` 在临时目录构造违规导入，验证报告确实指出文件。

### 行数与增长评审

`scripts/source_budgets.json` 只登记当前超过 600 行的九个重点文件，
为现状留少量余量；其他 `mewcode/**/*.py` 使用统一 600 行阈值，不需逐文件登记。
按 UTF-8 文本 `splitlines()` 计算物理行（含空行与注释），CRLF/LF 口径一致。
`--list` 显示每个文件的使用量、上限、具名/默认来源和剩余额度；删除或改名留下的条目会失败。

超过上限应先考虑拆分职责。确需提高具名 ceiling 或为跨过默认阈值的文件新增条目时，
在同一变更中说明新增职责、无法合理拆分的原因、原/新行数及上限、验证结果，供人工评审。
不要为单个文件增长提高全局默认值；不要自动刷新全部上限来消除失败。
文件显著缩小时应在同一评审中考虑收紧具名上限；本阶段仅执行当前上限比较，
不自动比较 Git 历史，也不声称每次删行都会自动收紧。

### 缺陷回植证据

回植脚本复制包、原回归文件及 conftest 到临时目录，禁用字节码缓存，
在独立解释器中确认导入路径来自临时副本；先测当前源码，再只修改临时副本。
不修改共享真实源码、不读真实配置，家目录也指向临时目录。
每个锚点必须唯一且替换后语法有效；缺少锚点立即失败。
pytest 插件记录实际测试 node ID、执行阶段、异常类型、末帧文件与断言源码。
RED 只接受指定现有回归的预期 AssertionError，其他失败、收集错误、跳过、超时均使验证失败。

| 回植项 | 现有回归 | 预期 RED 数量（本机） |
| --- | --- | --- |
| N02：移除 `%` 扩展限制 | test_permission_security::test_shell_expansion_and_unc_require_confirmation | 2 |
| N04：恢复仅依赖旧正则的删除检查 | test_permission_security::test_recursive_delete_option_order | 5 |
| N05：去掉 allow 规则复合命令保护 | test_permissions::TestDangerousCommandDetector::test_legacy_allow_rule_cannot_authorize_compound_shell | 5 |
| 容器凭据：把解析后的 key 写入配置 | test_service_sandbox::TestRunAgent::test_container_config_has_no_secret | 1 |

这是选定旧行为的定点回植，未逐字恢复历史提交，也不代表全部历史缺陷或任意变异覆盖。
CI 的平台矩阵运行架构/规模守卫和普通测试；security 作业额外运行回植脚本。
普通 pytest 包含拒绝伪 RED 证据的快速用例，耗时回植由独立命令运行。

### 本次验证结果

本机 Windows，Python 3.14.4，uv 0.11.7，基线 `1e8911e`，无业务源码改动或依赖变化。

- 全量 `uv run --locked pytest -q --timeout=120`：1377 passed / 18 skipped，231.45 秒。
- 最终定向 `uv run --locked pytest tests/test_engineering_guards.py tests/test_restored_defect_evidence.py -q --timeout=120`：
  22 passed（包含全量收集后补充的证据校验用例）。
- 最终回植命令：四项当前实现均 GREEN，恢复旧行为分别命中上述 2 / 5 / 5 / 1 个预期断言失败。
- 架构/行数守卫及 `--list` 通过；Ruff 全仓通过；全包 mypy：159 个源文件通过；锁文件检查通过。
- `git diff --check` 通过；本地运行日志保留于忽略目录 `.pytest_cache/engineering/`。

交接时遗留的 pytest 进程在清理前已自行退出，未取得输出，因此不计入本次通过证据；
上述全量结果来自确认其退出后启动的新运行。
跨平台 CI 尚未运行，真实 Docker 隔离未运行，跳过项不计为通过；未调用真实模型、未创建远端 PR。
实现验证阶段先保留本地改动供审阅；提交与推送按维护者后续明确指令执行。

不要读取、打印或提交包含凭据的实际配置；配置示例仅用于说明。
部署端仍需提供 Docker、模型凭据和 GitHub 认证。回放只在专用可丢弃仓库执行。
