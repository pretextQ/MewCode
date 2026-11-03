# 开发、维护与安全验证

先读[项目指令](../MEWCODE.md)、适用 AGENTS.md、[当前状态](../docs/README.md)及[维护待办](../docs/backlog.md)。核对 `git status`、暂存区、分支、HEAD 与实时远端；保留用户和并行修改，不 reset/stash、不强推。历史计划中的开发/推送授权不替代当前任务范围。

## 实现约束

- Python 3.11+、Textual、asyncio；英文 commit message，snake_case，PEP 604 类型标注。
- 遵守 [source_budgets.json](../scripts/source_budgets.json) 的现有 ceiling 和工程守卫。必要时拆模块，不为通过检查提高上限；service 不依赖 TUI，permissions 不依赖 service，tools/impl 不依赖 Agent。
- 保持权限 deny/危险检测/路径与写保护顺序、三客户端请求配对、typed memory、进程树取消与重复取消收尾、worktree 保留与恢复契约。
- Windows/Linux 是验收平台；默认黑色 TUI 已确认。未覆盖的真实 GUI 人工验收如实记录，不把 headless/PTY 自动检查当成人工完成。

## 测试隔离

没有新的真实模型预算。不得读取真实配置或记忆、启动真实用户 TUI；使用临时项目、临时 HOME/USERPROFILE、脚本 LLM、SDK 替身和本地 MCP。

`tests/conftest.py` 默认隔离 `Path.home()`、HOME 和 USERPROFILE；**这不替代 cwd/work_dir 和工具注册表绑定**。临时项目与临时 home 应分开，实际 Agent/工具注册表绑定临时 work_dir，子进程也须继承隔离环境。U1 因漏绑定而覆盖真实配置，仍未恢复；不能再次靠“有隔离夹具”推断路径安全。

先静态检查，再按受影响行为选择必要专项。权限、请求或共享生命周期变更按要求运行全量；纯文档整理做引用、路径依赖和 diff 检查即可，不为低影响改动新增镜像测试。记录真实平台、SHA、命令及 passed/failed/skipped，未执行项不能计通过。

## 工程检查入口

```powershell
uv run --locked ruff check .
uv run --locked mypy --platform win32 mewcode
uv run --locked mypy --platform linux mewcode
uv run --locked python scripts/engineering_guards.py
uv lock --check
git diff --check
```

涉及产品行为时按范围补充：

```powershell
uv run --locked pytest -q --maxfail=10 --timeout=120
uv run --locked python scripts/verify_restored_defects.py
uv run --locked python scripts/verify_subagent.py
uv run --locked pip-audit --local
uv build --no-sources
```

CI 配置见[工作流](../.github/workflows/ci.yml)：Windows/Linux × Python 3.11/3.13，加独立 security。只核验对应 SHA 的实际作业，不借用旧绿灯证明新改动。历史测试数量在[验收档案](../docs/acceptance/README.md)，不复制到常规开发说明。

## Docker、模型及回放

Docker 专项需要可用守护进程，按任务范围单独启用：

```powershell
$env:MEWCODE_DOCKER_TESTS = '1'
try {
    uv run --locked pytest tests/test_sandbox_live.py -q -ra --timeout=1200
} finally {
    Remove-Item Env:MEWCODE_DOCKER_TESTS
}
```

真机容器套件仍用假模型和测试后端；不等于真实模型效果。只核查本次测试命名空间的资源，不能全局清理 Docker 或未经核验的旧目录/卷，不为修绿测试开启宿主 fallback。终端离线协议检查和人工矩阵见[终端验收](../docs/acceptance/stage-2026-11-02-terminal.md)。

真实模型调用需要新的明确预算；2026-10-30 评测预算不延续。回放只用专用可丢弃仓库，脚本会修改仓库并可能推远端；no-push 仍会修改本地。先核对脚本参数、端口和配置契约，不输出密钥。

## 证据与文档

行为更新到相应指南，状态只更新[总入口](../docs/README.md)和[待办](../docs/backlog.md)，验收记录放 `docs/acceptance/`。原始日志、模型响应、截图和旧复现脚本按[证据索引](../docs/evidence/README.md)本地保留；旧缺陷复现成功不等于修复成功。未经语义复核、不完整和未覆盖的模型结果单列。

不提交实际配置、密钥或整片 evidence。旧测试环境文档曾出现明文凭据，已移除且未备份旧文本；如曾外传，应由持有者处理轮换，删除文本不能撤回泄露。当前配置事故事实见待办，不能引用旧历史中的“配置未改动”描述现状。
