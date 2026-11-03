# MewCode 项目

本文件用于在本仓库开发 MewCode 时向 Agent 提供项目约定。开始工作前阅读适用的 `AGENTS.md`、[开发指南](guides/development.md)、[当前状态](docs/README.md)和[维护待办](docs/backlog.md)，核对当前源码与任务范围；历史计划和验收记录不代表新的开发或推送授权。

## 项目与目录

MewCode 是共享 Agent 内核的 AI 编程助手与告警驱动修复服务，提供交互式 TUI、单次命令和无头服务入口。

- `mewcode/`：Agent、客户端、工具、权限、记忆与 TUI；`mewcode/service/`：告警受理、隔离修复、验证及 PR 交付。
- `tests/`：回归测试；`scripts/`：启动器、工程检查与验收工具。
- `guides/`：当前行为和运行契约；`docs/`：当前状态、待办、设计与验收证据。
- 使用与模型配置见 [README](README.md)，项目指令的加载范围见[项目指令指南](guides/project-instructions.md)。

## 技术栈

- Python 3.11+
- TUI 框架：Textual
- 异步：asyncio
- 依赖与环境管理：uv；构建及检查配置以 `pyproject.toml` 为准。

## 代码规范

- commit message 用英文
- 变量命名用 snake_case
- 类型标注用 PEP 604 语法（X | Y 而非 Union[X, Y]）
- 保持 service 不依赖 TUI、permissions 不依赖 service、tools/impl 不依赖 Agent。
- 遵守[源码规模上限](scripts/source_budgets.json)和工程守卫；需要时拆分模块，不为通过检查提高上限。
- 修改共享内核时保持权限判定与写保护、客户端请求配对、类型化记忆、进程树取消收尾以及 worktree 保留与恢复契约。详细行为见[指南索引](guides/README.md)。

## 开发与验证

从仓库根目录安装依赖、启动：

```powershell
uv sync --locked
uv run mewcode
```

Windows 使用带终端恢复的短启动器；第二条将指定文件夹作为工作目录，默认沿用本仓库的 `.mewcode/config.yaml`（如存在）：

```powershell
.\mew
.\mew "D:\projects\你的项目"
```

先做静态检查，再按受影响行为运行必要专项；权限、客户端请求或共享生命周期变更按开发指南运行全量。常用检查入口：

```powershell
uv run --locked ruff check .
uv run --locked mypy --platform win32 mewcode
uv run --locked mypy --platform linux mewcode
uv run --locked python scripts/engineering_guards.py
uv lock --check
git diff --check
uv run --locked pytest -q --maxfail=10 --timeout=120
```

测试使用临时项目、独立临时 HOME/USERPROFILE、替身模型和本地 MCP；Agent、工具注册表及子进程均须绑定临时工作目录。仅隔离 home 不足以保护真实项目。不得在测试中读取或覆盖真实配置、记忆、会话，或启动真实用户 TUI；真实模型与 Docker 验证按当前任务授权和[开发指南](guides/development.md)执行，不继承历史模型预算。

Windows/Linux 是验收平台；记录实际执行的命令与结果，未执行或跳过的检查单列，headless/PTY 自动测试不替代 GUI 人工验收。纯文档改动核对链接、引用和 diff 即可。

## 协作与文档

- 修改前核对 Git 分支、HEAD、暂存区、工作区及实时远端；保留用户与并行修改，不擅自 reset、stash 或强推。
- 不提交实际配置、密钥或整片原始证据；不清理未经核验归属的目录或 Docker 资源。
- 行为更新到 `guides/`，当前状态更新到 `docs/README.md`，待办维护在 `docs/backlog.md`，验收记录放 `docs/acceptance/`。新增或移动文档时同步入口和路径引用。
- 本文件保留稳定开发约定；具体历史过程、测试数字和临时任务进度放入对应文档。
