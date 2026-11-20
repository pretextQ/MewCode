# 服务验证与修复反馈验收

2026-11-18 的本地验收环境为 Windows、Python 3.14.4。pytest 在临时项目和隔离 HOME/USERPROFILE 中运行，修复者使用脚本替身。
端到端用例使用本地 bare Git 远端、GitHub API 替身和集成验证替身。工程检查使用主仓库的锁定依赖。

现行报告字段、测试命令识别与重试配置见 [服务验证契约](../../guides/service-reliability.md#本地验证与发布版本) · [服务监督配置](../../guides/supervision.md#服务-dead-end-反馈2026-11-01)。

| 交付内容 | 实现提交 |
| --- | --- |
| 发布提交与本地验证源码树 | `b494bdd` |
| pytest 验证范围 | `72db6ab` |
| pytest dead-end 重试反馈 | `e9ceb2e` |

## 发布提交与本地验证源码树

- 自动回归覆盖单测和集成验证使用同一源码树，以及 PR、任务报告和实际推送 SHA 的对应关系。
- 在集成验证结束、发布入口及提交阶段分别插入源码修改。报告记录候选树与失效状态，服务升级人工，远端没有产生该修复分支。
- `pytest -q --maxfail=10 --timeout=120` 运行 service execution、e2e、integration、vcs、policy、metrics、durability 和 independent review 套件，304 通过。
  首轮发现 CI 门禁未保留独立审查 SHA 的回退路径，恢复兼容后复跑通过。复跑产生一条测试类收集警告，提交中通过测试导入别名消除。
- 工程检查通过 `ruff check .`、Windows/Linux mypy、`scripts/engineering_guards.py`、`uv lock --check` 和 `git diff --check`。

实现参考 [ZMAI CompletionState](https://github.com/lip20041219/zmai/blob/d18d1ae/src/zmai/swe/completion.py) 的验证失效规则。
本地检查未覆盖真实 GitHub PR、Docker 集成依赖、真实模型或 Linux 运行时。

## pytest 验证范围

- 以相同 pytest 命令复验配置缩小范围、仅收集、删除测试、跳过和改写断言。原验收失败而候选命令退出为 0 时，PR 与报告仍要求核对范围差异。
- 在既有文件追加回归用例保留 `preserved`，复合 shell 命令在报告中明确显示证据不可用。
- 用人工返工复核原基线持久化，验证决定接口拒绝缺少 SHA 或 SHA 过期的确认，并在正确 SHA 与原因同时提供时记录合并决定。
- 检查沙箱替身收到探针参数，runner 从测试输出剥离取证数据并清理临时模块。
- `pytest -q --maxfail=10 --timeout=120` 运行服务、独立审查及新增范围套件，472 通过。`ruff check .`、Windows/Linux mypy、工程守卫、锁文件与 diff 检查通过。

取证点参考 [ZMAI pytest 验收守卫](https://github.com/lip20041219/zmai/blob/d18d1ae/src/zmai/swe/acceptance_guard.py)。
这轮覆盖原 pytest 进程的收集结果与运行报告；Docker 沙箱传输使用替身，分布式和集成测试范围未做前后对照。

## pytest dead-end 重试反馈

- 让真实 pytest 连续报出相同失败，将节点 ID、断言的实际与期望值、traceback 行号和标明推断的候选文件传给下一次修复替身。
- 回归覆盖同名参数化用例块的关联、歧义的回退、长捕获输出下的反馈上限，以及 TDD、环境、未知输出和重试/预算兼容。
- 复用原 execution、attempt 与完整失败指纹。任务报告只对已准备的下一次提醒保存简明诊断。
- `pytest -q --maxfail=10 --timeout=120` 运行服务执行、端到端、pytest 范围、恢复、失败反馈及监督套件，232 通过。`ruff check .`、Windows/Linux mypy、工程守卫、锁文件与 diff 检查通过。

解析策略参考 [ZMAI 的失败解析](https://github.com/lip20041219/zmai/blob/d18d1ae/src/zmai/swe/failure.py)。输出是定位线索，模型采纳和生产效果仍待实测。

## CI 依赖审计修复

三次服务变更的 `security regression` 都在 `pip-audit --local` 检出锁定的 `opentelemetry-api 1.45.1` 受 CVE-2026-126360 影响。
锁文件将该间接依赖更新到修复版本 `1.45.2`，wheel 和 sdist 散列已与 PyPI 发布记录核对。审计步骤与失败条件保持启用。
[CI #197 审计日志](https://github.com/pretextQ/MewCode/actions/runs/44297379766/job/134777841323) · [上游修复公告](https://github.com/open-telemetry/opentelemetry-python/security/advisories/GHSA-mf5v-w3w3-f6cg)

CI #195 的 Linux/Python 3.11 另有一次进程清理测试失败，断言打印时三个进程均已退出。CI #197 的两个 Linux 矩阵作业均通过，偶发失败的根因仍待确认。[CI #195](https://github.com/pretextQ/MewCode/actions/runs/44293175312) · [CI #197](https://github.com/pretextQ/MewCode/actions/runs/44297379766)

隔离的 Windows/Python 3.11.9 环境中，权限安全回归 296 通过、3 跳过，四项缺陷回植均得到预期的红绿结果。
本地首次将 `--basetemp` 放进环境变量，影响嵌套 pytest；复跑仅给外层 pytest 传参。
`uv lock --check` 和 `pip-audit --local` 均成功。审计未报告已知漏洞，并注明本地 MewCode 包在 PyPI 上无记录。

2026-11-20 核对 GitHub Actions 时，依赖修复提交 `2738fa1` 的 CI #198 已通过。随后主分支 `a1a0c91` 对应的 CI #205 也已通过，覆盖 Windows 和 Ubuntu 的 Python 3.11、3.13 测试矩阵及独立安全回归。[CI #198](https://github.com/pretextQ/MewCode/actions/runs/44298841393) · [CI #205](https://github.com/pretextQ/MewCode/actions/runs/44307297888)
