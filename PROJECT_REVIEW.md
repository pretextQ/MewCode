# MewCode 项目审查报告

> 本文记录修复前的审查基线。R01～R09 的后续修复与验证记录见 [REVIEW_FIXES.md](REVIEW_FIXES.md)。

- 审查日期：2026-10-15
- 审查对象：当前工作区项目代码
- 重点范围：权限边界、文件工具、服务执行链、容器生命周期、配置传递、回滚与重试
- 本次操作：代码阅读、全量测试、静态检查和针对性复现；未修改业务代码

## 审查结论

本次发现 9 个建议修复的问题，其中 P1 问题 5 个、P2 问题 4 个。

- **P1**：优先修复，涉及隔离边界、未经确认的修改操作或并发执行可靠性。
- **P2**：后续修复，涉及状态一致性、配置行为和成本控制。

现有测试全部通过，但尚未覆盖下述触发场景。容器相关问题通过测试替身与命令捕获验证，未执行真实容器越权测试；报告不代表已经在真实 Docker 环境完成攻击验证，也不保证穷尽项目中的所有问题。

## 问题总览

| 编号 | 优先级 | 问题 | 主要位置 |
| --- | --- | --- | --- |
| R01 | P1 | Compose 验证可绕过 agent 容器隔离 | `mewcode/service/compose.py:128` |
| R02 | P1 | Grep、Glob 可通过搜索模式越界访问文件 | `mewcode/tools/grep.py:18` |
| R03 | P1 | Git 删除命令被误判为安全只读命令 | `mewcode/permissions/dangerous.py:72` |
| R04 | P1 | 并发 job 的测试共用容器名 | `mewcode/service/execution.py:356` |
| R05 | P1 | 任务取消时未停止 Docker 容器 | `mewcode/service/sandbox.py:481` |
| R06 | P2 | 检查点复用编辑前备份，导致回滚状态错位 | `mewcode/filehistory/history.py:58` |
| R07 | P2 | 服务模式部分忽略 `--config` | `mewcode/__main__.py:76` |
| R08 | P2 | 沙箱未传递 provider 实际解析出的 API key | `mewcode/service/sandbox.py:428` |
| R09 | P2 | 单 job token 预算未累计此前重试消耗 | `mewcode/service/execution.py:529` |

行号对应本次审查时的代码，后续修改可能导致偏移。

## 详细发现

### R01 · [P1] Compose 验证可绕过 agent 容器隔离

**位置**：`mewcode/service/compose.py:128–135`。

**触发条件与原因**：仓库配置了集成测试命令、容器运行时可用，且 worktree 中存在 Compose 文件。验证器直接让宿主 Docker 执行该文件，而文件内容可以由 agent 修改，未校验权限和挂载声明。

**影响**：Compose 文件可以声明特权容器、宿主根目录挂载或 Docker socket 挂载，从而突破 agent 沙箱的隔离边界。

**验证证据**：使用包含 `privileged: true` 和宿主根目录挂载声明的 Compose 文件，命令捕获确认验证器仍会将其交给宿主运行时执行 `compose up`。未实际启动该容器。

**修复建议**：使用受信任的编排配置，并校验最终生效的 Compose 配置；限制特权模式、宿主挂载、Docker socket 和其他会扩大宿主访问能力的选项。避免直接信任 agent 修改后的编排文件。

### R02 · [P1] Grep、Glob 可通过搜索模式越界访问文件

**位置**：`mewcode/tools/grep.py:18–28`；`mewcode/tools/glob.py` 的匹配结果处理；权限入口为 `mewcode/permissions/checker.py`。

**触发条件与原因**：搜索目录位于允许范围内，但搜索模式含有 `..`，例如 Grep 的 `include="../outside/*.txt"`。权限检查只验证搜索目录，工具读取匹配结果前未再次检查实际路径。

**影响**：Grep 可以读取沙箱外的文件内容；Glob 可以列出沙箱外的匹配文件。符号链接目标同样需要纳入路径检查。

**验证证据**：在隔离的临时测试目录中，将允许的临时目录范围显式收窄后，确认外部目标的直接路径检查被拒绝，但 Grep 仍能返回该文件内容，Glob 仍能列出该文件。

**修复建议**：对每个匹配结果解析真实路径，并在读取或返回前使用相同的沙箱规则验证。仅过滤搜索目录不足以保证边界。

### R03 · [P1] Git 删除命令被误判为安全只读命令

**位置**：`mewcode/permissions/dangerous.py:72–74`。

**触发条件与原因**：安全命令使用前缀匹配；`git branch`、`git tag` 被列为安全前缀，但这些子命令的部分参数会修改仓库状态。

**影响**：默认权限模式下，模型可以未经确认删除本地分支或标签。

**验证证据**：以下命令的权限判定均为 `allow`，原因为 `Safe read-only command`；复现仅调用权限判定，未实际执行删除。

```text
git branch -D important-branch
git tag -d release-v1
```

**修复建议**：按子命令和参数判断只读性，显式排除删除、重命名和其他修改选项；无法确定只读的形式应进入正常权限流程。

### R04 · [P1] 并发 job 的测试共用容器名

**位置**：`mewcode/service/execution.py:355–357`；服务组装处为 `mewcode/__main__.py`。

**触发条件与原因**：服务共享一个 `SandboxTestRunner`，该执行器将固定的 `repo_name` 作为 `run_command` 的 job ID。多个 job 因此生成相同的测试容器名。

**影响**：启动前的旧容器清理会执行 `rm -f`，可能删除另一个 job 正在运行的测试容器，导致并发验证互相干扰。

**验证证据**：并发调用两个不同工作目录的测试，捕获到两次调用传入的 job ID 均为相同的仓库名。

**修复建议**：让测试执行接口接收实际 job ID，并使用 job ID 生成容器名；仓库名应独立传递，服务也不应将所有仓库测试固定标记为第一个仓库。

### R05 · [P1] 任务取消时未停止 Docker 容器

**位置**：`mewcode/service/sandbox.py:481–488`，以及 `run_command` 中的对应执行路径。

**触发条件与原因**：Worker 总超时或服务退出触发 `CancelledError`。当前代码只在宿主命令返回 124 时调用停止容器，取消异常会直接跳过后续清理。

**影响**：停止 Docker 客户端进程不保证停止由 daemon 管理的容器。容器可能继续消耗资源、运行 agent 或修改 worktree。

**验证证据**：在替身运行器进入 `docker run` 后取消任务，命令捕获中仅有启动前的 `rm -f` 和 `run`，没有取消后的停止或删除容器操作。

**修复建议**：为 `run_agent` 和 `run_command` 增加显式取消清理；清理应能承受再次取消、有合理时限，并在完成后继续传播原始取消信号。

### R06 · [P2] 检查点复用编辑前备份，导致回滚状态错位

**位置**：`mewcode/filehistory/history.py:58–67`；检查点创建调用见 `mewcode/agent.py:617` 和 `mewcode/agent.py:1334`。

**触发条件与原因**：`track_edit` 保存修改前内容，agent 在整轮完成后创建检查点；`make_snapshot` 却复用已有的修改前备份，没有保存检查点时的实际内容。

**影响**：文件恢复后的状态与检查点对应的对话不一致，后续编辑可能基于错误现场。

**验证证据**：按“修改前备份 → 第一轮修改 → 第一轮检查点 → 第二轮修改 → 第二轮检查点”的顺序操作，回滚到第一轮检查点后，文件内容为第一轮修改前的 `before`，而非第一轮完成后的 `after-first-turn`。

**修复建议**：区分编辑前撤销备份与轮次检查点；检查点应保存当时的文件内容和存在状态，并明确处理检查点后首次出现的文件。

### R07 · [P2] 服务模式部分忽略 `--config`

**位置**：`mewcode/__main__.py:75–87`。

**触发条件与原因**：通过 `mewcode serve --config <文件>` 启动时，入口已加载指定配置，但 `_serve` 再次调用无参数的 `load_config()`。

**影响**：服务设置来自指定配置，provider、hooks 等却可能来自默认配置，导致使用错误的模型、认证信息或行为配置。

**验证依据**：代码调用链确认 `_serve_main` 仅将 `service` 配置传入 `_serve`，后者重新发现并加载默认配置。

**修复建议**：将入口解析出的完整 `AppConfig` 传入 `_serve`，复用同一份配置；增加指定配置与默认配置不同的入口回归测试。

### R08 · [P2] 沙箱未传递 provider 实际解析出的 API key

**位置**：`mewcode/service/sandbox.py:428–436`；容器配置生成见 `write_container_config`。

**触发条件与原因**：provider 使用配置中的字面量 key 或 `${CUSTOM_KEY}`。容器配置会清空 `api_key`，而环境变量传递只复制标准变量及透传白名单，没有注入 `resolve_api_key()` 的实际解析结果。

**影响**：同一 provider 在宿主有效，在容器中可能缺少认证信息，或误用另一个标准环境变量中的 key。

**验证证据**：provider 配置引用自定义环境变量时，宿主能够正确解析测试 key，但容器环境中的协议标准 key 与解析结果不一致。

**修复建议**：将 provider 实际解析出的 key 注入协议对应的容器环境变量，保持宿主与容器的认证优先级一致；继续避免将 key 写入生成的配置文件或日志。

### R09 · [P2] 单 job token 预算未累计此前重试消耗

**位置**：`mewcode/service/execution.py:529`；直跑路径见同文件 `:607`。

**触发条件与原因**：同一 job 发生修复或 CI 失败重试。当前检查仅比较本次 agent 运行用量与完整预算，没有扣除此前 attempt 的用量。

**影响**：各次运行均低于预算时，job 累计消耗仍可超过声明的单 job 上限。

**验证证据**：预算设为 100，job 已消耗 60，本次沙箱运行再消耗 60，预算检查仍通过，累计达到 120。

**修复建议**：基于 JobStore 中最新累计用量计算剩余预算，并将剩余额度用于直跑熔断和沙箱事后门禁；超预算退出时也应保留已发生用量的审计记录。

## 验证结果

| 检查 | 结果 |
| --- | --- |
| 全量 pytest | **1242 passed，16 skipped**，耗时约 176 秒 |
| Ruff | 通过 |
| CI 范围内 mypy | 通过，检查 32 个源文件 |
| 全包 mypy 尝试 | 未完成：`backend-interview` 目录含 `__init__.py`，但目录名不是合法 Python 包名 |
| 针对性复现 | 权限误放行、搜索越界、固定测试容器 ID、取消缺少容器清理、API key 传递、预算累计与回滚错位 |
| Docker 实机验证 | 未运行；Compose 与容器生命周期使用替身和命令捕获验证 |

实际执行的检查命令：

```powershell
.\.venv\Scripts\python.exe -m pytest -q --tb=short
.\.venv\Scripts\ruff.exe check .
.\.venv\Scripts\mypy.exe mewcode/tools mewcode/permissions mewcode/serialization.py
```

## 建议修复顺序

1. 修复隔离与权限边界：R01、R02、R03。
2. 修复并发与取消清理：R04、R05。
3. 修复回滚一致性：R06。
4. 统一配置与认证传递：R07、R08。
5. 完善累计预算控制：R09。

为每项修复补充能重现实际触发条件的回归测试。容器隔离与生命周期修复还需要在真实 Docker 环境验证。
