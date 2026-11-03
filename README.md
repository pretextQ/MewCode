# MewCode

[![CI](https://github.com/pretextQ/MewCode/actions/workflows/ci.yml/badge.svg)](https://github.com/pretextQ/MewCode/actions/workflows/ci.yml)
![Python](https://img.shields.io/badge/python-3.11%2B-blue)
[![XERJ Corpus Hub contributor](https://hub.xerj.org/contributors/pretextQ/badge.svg)](https://hub.xerj.org/contributors/pretextQ)

**MewCode 是一个共享 Agent 内核的 AI 编程助手与告警驱动修复服务。**你可以在终端中阅读、修改和验证代码，也可以将告警转成可追溯任务，在独立 worktree 和 Docker 容器中尝试修复，再生成 PR 交给人工审查。服务不执行远端合并。

```text
交互式 TUI / 单次命令 ───────────────────▶ Agent 内核 → 工具、权限与上下文
                                              ▲
Alertmanager / 手动触发 → 去重与任务队列 → 独立 worktree / Docker
                                              │
                                      修复与验证 → PR → CI → 人工审查
```

[快速开始](#快速开始) · [服务模式](#告警驱动服务) · [安全与验证边界](#安全与验证边界) · [完整文档](docs/README.md)

## 能做什么

| 场景 | 当前能力 | 说明 |
| --- | --- | --- |
| 终端开发 | 文件读写与编辑 diff、Shell、Glob/Grep、上下文压缩、会话恢复、子代理与任务管理 | CLI、TUI 与服务共享执行内核 |
| TUI 工作台 | 黑色主题、流式与历史消息、F2 开发详情、按需 Git 状态/diff、取消与退出清理 | [交互与快捷键](guides/tui.md) |
| 权限与项目约束 | 分层权限、配置/技能文件写保护、拒绝授权时附反馈、目录链项目指令 | [权限语义](guides/permission-semantics.md)、[项目指令](guides/project-instructions.md) |
| 工具扩展 | 内置/项目 Skills、stdio/Streamable HTTP MCP、legacy/eager/dispatch/auto 加载 | [MCP 加载与分发](guides/mcp-loading.md)，默认 legacy |
| 告警修复 | Alertmanager/手动接入、SQLite 原子去重、有界队列、独立 worktree、修复验证、PR 与 CI 观察 | [架构与生命周期](guides/architecture.md) |
| 服务交接 | 通知 Outbox、策略快照与重验证、execution 记录、人工 decision、重启恢复 | [服务可靠性](guides/service-reliability.md) |
| 可选审查与监督 | 固定提交独立审查、循环预算预警、dead-end、self-verify、intent anchor | 默认关闭；[独立审查](guides/independent-review.md)、[监督指南](guides/supervision.md) |
| 诊断与复盘 | 追加/轮转日志、进程异常堆栈、Prometheus 指标、逐任务报告及 token 统计 | [进程诊断](guides/diagnostics.md) |

## 快速开始

需要 Python 3.11+、Git 和 [uv](https://docs.astral.sh/uv/)。Docker 用于服务的默认隔离执行；普通 TUI/单次命令在当前宿主运行。

### 1. 获取项目

```bash
git clone https://github.com/pretextQ/MewCode.git
cd MewCode
uv sync --locked
```

### 2. 配置模型

在项目内创建 `.mewcode/config.yaml`，先填写与你的模型端点相符的 `protocol`、`base_url` 和 `model`：

```yaml
providers:
  - name: my-provider
    protocol: anthropic
    base_url: https://your-provider.example/anthropic
    model: your-model-name
    api_key: "${MEWCODE_MODEL_API_KEY}"

permission_mode: default
mcp_loading: legacy
mcp_servers: []
enable_supervision: false
```

上面的地址和模型名是占位符，需要替换。协议由实际端点决定：

| `protocol` | 本项目使用的接口 | `api_key` 留空时读取 |
| --- | --- | --- |
| `anthropic` | Messages | `ANTHROPIC_API_KEY` |
| `openai` | Responses | `OPENAI_API_KEY` |
| `openai-compat` | Chat Completions | `OPENAI_API_KEY` |

`${MEWCODE_MODEL_API_KEY}` 使用自定义环境变量；例如在当前终端设置：

```powershell
# PowerShell
$env:MEWCODE_MODEL_API_KEY = "填入你的密钥"
```

```bash
# Bash
export MEWCODE_MODEL_API_KEY="填入你的密钥"
```

配置发现顺序为 `~/.mewcode/config.yaml` → 当前目录 `.mewcode/config.yaml` → `.mewcode/config.local.yaml`，后层按字段合并；也可用 `--config <path>` 显式指定单份配置。`api_key` 字面量优先于环境变量。更多字段见[配置示例](.mewcode/config.example.yaml)，真实配置与凭据不要提交到仓库。

### 3. 启动 TUI

```bash
uv run mewcode
```

Windows 从项目根目录使用带父终端恢复的启动器：

```powershell
powershell -NoLogo -NoProfile -File .\scripts\run_tui.ps1
```

也可以在 MewCode 根目录使用短命令，打开自身或指定文件夹：

```powershell
.\mew
.\mew "D:\projects\你的项目"
```

指定文件夹时默认使用 MewCode 仓库的 `.mewcode/config.yaml`；可用 `-Config "配置文件路径"` 指定其他配置。目标路径支持空格和中文。

| 操作 | 按键 |
| --- | --- |
| 发送 / 换行 | Enter / Shift+Enter 或 Ctrl+J |
| 开关开发详情 | F2 |
| 返回最新消息 | Ctrl+End |
| 展开/收起工具输出 | Ctrl+O |
| 停止 / 空闲退出 | 运行中 Ctrl+C 请求停止；空闲时 Ctrl+C 退出 |

停止会等待工具收尾，已经完成的文件修改会保留。Git 状态和 diff 按需读取，不会随流式输出反复刷新；详情可能包含启动前已有改动。完整交互与平台边界见[TUI 指南](guides/tui.md)。

### 单次命令与权限模式

```bash
uv run mewcode -p "概括这个项目的结构与主要入口" --output-format json
```

JSON 包含 `result`、`usage`、`toolCalls`、MCP 使用信息和 `sessionId`；启用监督时附加监督报告。它是运行摘要，不能单凭返回文本判断修复或测试成功。

无人值守修复可显式选择 `dontAsk`：

```bash
uv run mewcode -p "修复 calc.py 的失败测试并运行相关验证" --mode dontAsk --output-format json
```

`default` 对常规写入与命令请求确认；`acceptEdits` 自动接受常规文件编辑；`dontAsk` 在模式兜底时允许常规读、写与命令，仍保留显式 deny、危险命令检测、路径约束和文件写保护。非交互入口无法完成 UI 确认时会拒绝对应操作。权限模式提供应用层门禁，OS 隔离由服务容器承担；完整模式与判定顺序见[权限指南](guides/permission-semantics.md)。

## 告警驱动服务

服务面向已配置的仓库、模型和 GitHub 远端。先准备仓库 checkout、可用的容器运行时、推送/建 PR 所需凭据，以及仓库自己的测试命令。

### 配置与启动

在模型配置同一文件中增加 `service:`，替换仓库路径和远端：

```yaml
service:
  host: 127.0.0.1
  port: 9300
  webhook_token: "${MEWCODE_WEBHOOK_TOKEN}"
  token_budget: 500000
  vcs:
    provider: github
    token: "${GITHUB_TOKEN}"
    base_branch: main
  repos:
    demo:
      path: /absolute/path/to/demo-repo  # Windows 可用 D:/projects/demo-repo
      url: https://github.com/you/demo-repo.git
      base_branch: main
      test_command: python test_app.py
      # 仓库配置了受支持的 Compose 依赖时可启用：
      # integration_test_command: python test_integration.py
  sandbox:
    enabled: true
    allow_host_fallback: false
  mcp_loading: legacy
```

`test_command` 需替换为该仓库中可执行的验证命令，模型与 GitHub 凭据分别用于各自调用。设置示例中的环境变量后启动：

```bash
uv run mewcode serve --port 9300
```

正常交付路径为 `received → triaging → reproducing → fixing → verifying → pr_opened → ci_gate → human_review`。验证或 CI 失败按预算有界重试，默认最多三次修复尝试；无法完成时升级人工，无改动可收敛为 `cant_repro`。等待人审的任务不会因服务重启自动重跑。

仓库测试命令与远端 CI 均按配置执行；未配置、不可用或跳过的验证必须单独记录。PR 生成和 `human_review` 状态表示交付人工评审，不表示已经合并、上线或生产恢复。

### 接入、人工决策与观测

| 端点 | 用途 |
| --- | --- |
| `POST /webhook/alert` | Alertmanager 告警接入 |
| `POST /webhook/manual` | 手动 JSON 触发 |
| `GET /jobs`、`GET /jobs/{id}/report` | 任务查询、执行记录、策略、通知与验证证据 |
| `POST /jobs/{id}/decision` | 记录 `merged`、`changes_requested` 或 `escalate` |
| `GET /metrics` | Prometheus 状态、仓库计数与 alert→PR 耗时指标 |
| `GET /costs?repo=demo` | 聚合 provider 原始 token 数量，不换算费用 |
| `GET /healthz` | 最小健康检查 |

配置 `service.webhook_token` 后，除最小健康探针外的端点均要求 `X-MewCode-Token` 或 `Authorization: Bearer`。匿名 `/healthz` 只返回 `status`；带有效 token 才返回详细状态。未配置 token 时只允许回环监听。

人工决策 JSON 示例为 `{"decision":"changes_requested","reason":"请补充边界测试"}`；修改请求重新入队，`merged` 记录人工结论，端点不调用远端合并。关键通知与状态同事务登记，Outbox 独立重试；重启补发通知不会重跑 Agent。

仓库可通过 `<repo>/.mewcode/policy.yaml` 覆盖严重度路由、目标分支、预算和通知。任务保存脱敏策略快照，执行与发布前重验证；详情见[服务交接与恢复](guides/service-reliability.md)。服务只挂载声明只读的 MCP 工具，内置 Loki/GitHub 查询服务使用 GET；第三方声明仍需独立判断。

### 演示与回放

专用演示仓可使用 [m1_demo.py](scripts/m1_demo.py) 初始化测试场景和发送模拟告警；[m3_replay.py](scripts/m3_replay.py) 批量回放并生成 JSON/Markdown 对照报告。这些脚本会修改演示仓库，回放还可能推送远端；只在专用可丢弃仓库上运行，并另行设置模型预算。

[历史演示 PR](https://github.com/pretextQ/mewcode-alert-demo/pull/5)保留了结构化交付示例。2026-10-05 两轮各三个样本均到达人审，追溯标记从 0/3 变为 3/3；样本及指标见[历史回放摘要](docs/history/replay-2026-10-05.md)。该回放使用 direct 路径、集成验证跳过，不与其他 Docker 真机测试合并表述；小样本没有证明生产成功率、成本或延迟改善。alert→PR 也不等于完整故障 MTTR。

## 安全与验证边界

- 默认服务 Docker 沙箱采用非 root、capability 丢弃、禁止提权、只读根、资源限额和硬超时。Docker 不可用时默认拒绝执行；显式关闭沙箱或开启宿主回退会改变隔离边界。普通 CLI/TUI 的 Shell 在宿主运行。
- 容器网络提供 `bridge` / `none`，未内置细粒度域名出口白名单。Compose 仅支持经过验证的安全子集，不能将任意仓库 Compose 文件直接视为可信环境。
- MCP legacy 保持默认；eager、dispatch 与显式 auto 已实现，native/tool_reference 和真实缓存收益仍待独立验证。MCP 分发继续执行真实目标权限、角色、hooks 和参数验证。
- 独立审查与监督默认关闭。已有执行循环预算预警，token 预警、GitHub 评论/rework 闭环、PR 发布队列持久化和 observe 仍为候选；功能送达或工具成功不证明模型采纳和修复效果。
- Windows/Linux 是当前验收目标。真实 PTY 自动测试不替代 GUI 终端人工验收，Linux tree-service 偶发回收失败根因仍未确认；其他维护事项见[维护待办](docs/backlog.md)。

工程检查、真实容器测试、模型评估与人工验收分别记录，不用某一类结果替代其他类。U1–U7、A1–A5 已交付，具体提交、失败过程与 CI 以[验收档案](docs/acceptance/README.md)为准；本页不复制容易过期的测试总数。

## 开发与文档

```bash
uv run --locked pytest -q --maxfail=10 --timeout=120
uv run --locked ruff check .
uv run --locked python scripts/engineering_guards.py
```

默认测试使用模型与外部服务替身，并在临时目录中验证真实文件、worktree 和子进程。真实 Docker 专项需显式启用 `MEWCODE_DOCKER_TESTS=1`，真实模型调用需独立配置和预算；隔离约束、完整检查与复现入口见[开发指南](guides/development.md)。

| 文档 | 内容 |
| --- | --- |
| [指南索引](guides/README.md) | TUI、权限、项目指令、MCP、hooks、监督、诊断与平台约定 |
| [当前状态与文档结构](docs/README.md) | 已交付能力、维护入口和文档组织规则 |
| [维护待办](docs/backlog.md) | 未解决问题、人工验收与尚未授权的候选方向 |
| [设计与演进](docs/evolution/README.md) | 已交付方案和后续提案 |
| [验收与历史](docs/acceptance/README.md) | 各期测试、提交、CI、模型评估与验证边界 |

主要目录：`mewcode/` 为运行内核、工具与服务，`tests/` 为回归测试，`scripts/` 为检查和演示入口，`guides/` 为行为契约，`docs/` 为状态、设计、验收和历史。贡献前先读 [MEWCODE.md](MEWCODE.md) 与适用的 AGENTS.md。

许可证：[MIT](LICENSE)。
