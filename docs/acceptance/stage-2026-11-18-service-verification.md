# 服务验证与修复反馈验收

环境为 Windows、Python 3.14.4。pytest 在临时项目和隔离 HOME/USERPROFILE 中运行，修复者使用脚本替身。
端到端用例使用本地 bare Git 远端、GitHub API 替身和集成验证替身。工程检查使用主仓库的锁定依赖。

## 发布提交与本地验证源码树

- 自动回归覆盖单测和集成验证使用同一源码树，以及 PR、任务报告和实际推送 SHA 的对应关系。
- 在集成验证结束、发布入口及提交阶段分别插入源码修改。报告记录候选树与失效状态，服务升级人工，远端没有产生该修复分支。
- `pytest -q --maxfail=10 --timeout=120` 运行 service execution、e2e、integration、vcs、policy、metrics、durability 和 independent review 套件，304 通过。
  首轮发现 CI 门禁未保留独立审查 SHA 的回退路径，恢复兼容后复跑通过。复跑产生一条测试类收集警告，提交中通过测试导入别名消除。
- 工程检查通过 `ruff check .`、Windows/Linux mypy、`scripts/engineering_guards.py`、`uv lock --check` 和 `git diff --check`。

实现参考 [ZMAI CompletionState](https://github.com/lip20041219/zmai/blob/d18d1ae/src/zmai/swe/completion.py) 的验证失效规则。
本地检查未覆盖真实 GitHub PR、Docker 集成依赖、真实模型或 Linux 运行时。
