# MewCode

[![CI](https://github.com/pretextQ/MewCode/actions/workflows/ci.yml/badge.svg)](https://github.com/pretextQ/MewCode/actions/workflows/ci.yml)
![Python](https://img.shields.io/badge/python-3.11%2B-blue)

**MewCode 是一个 AI Coding Agent，也是一套告警驱动的自动化开发服务**：同一个 agent 内核，既能作为交互式 CLI 帮你写代码，也能以无头服务的形式常驻——线上告警自动触发，agent 在 OS 级沙箱里定位 bug、写修复、跑测试（含自起 docker-compose 依赖环境的集成验证），以 PR 形式交给人工 review。**人审 PR 是唯一必经的人工点，服务没有任何 merge 权限。**服务暴露 Prometheus 指标与逐单复盘报告，评估集回放让每次提示词/内核改动都能量化对比。

```
告警(Alertmanager) ─▶ 服务层 ─▶ 每 job 隔离单元 ─▶ PR + CI 门禁 ─▶ 人工 review
                     webhook      沙箱容器/          结构化证据链      唯一上线门禁
                     job 状态机   git worktree       （告警摘要/根因/
                     重试与去重   agent 内核          测试前后对比/集成验证）
```

## 它解决什么问题

有线上服务就有告警，值班处理的问题里大量是模式化的（配置错、空指针、依赖超时）。MewCode 把"收到告警 → 定位 → 修复 → 验证 → 提交 review"这条链路自动化：

- **告警进来，PR 出去，全程无人干预**——真实 demo 仓库（[pretextQ/mewcode-alert-demo](https://github.com/pretextQ/mewcode-alert-demo)）里保留着历次无人值守运行的 PR，每份 PR body 都是结构化证据链：告警摘要、根因分析、修复说明、测试前后对比、集成验证结果、规范自查。可从 [PR #5](https://github.com/pretextQ/mewcode-alert-demo/pull/5) 看起。
- **修不好就诚实升级**：重试次数有上限，超限进入 escalate 并附上已尝试的分析，绝不产生垃圾 PR；验证不过的修复不会被发布。
- **运营可量化**：评估集回放实测（三类真机 bug × 前后两轮，6 单全部无人值守跑通）——修复 PR 成功率 100%，告警→PR 中位 25–26 秒（p90 29–43 秒），单 job token 7.7k–10.8k；给代码规范加一条"可追溯注释"规则后，PR diff 的规则命中从 0/3 变 3/3 而成功率与成本不变（详见[评估集回放](#运营化指标复盘与评估集回放)）。

## 核心特性

**执行内核（CLI / TUI）**
- 工具集：文件读写编辑、Bash、Glob/Grep、子代理、任务管理；权限分层管线（危险命令检测 → 路径沙箱 → 规则引擎 → 模式判定），deny 优先于白名单
- Skill 机制（内置 incident-triage / org-code-style 等，仓库级 `.mewcode/skills/` 可覆盖）与 MCP 接入（stdio / Streamable HTTP）
- 上下文压缩、任务状态机、hook 引擎；Windows 是一等公民平台（见[平台说明](#平台说明)）

**服务化（headless）**
- Alertmanager webhook / 手动 JSON 触发，SQLite JobStore + 显式状态机 + 指纹去重，优雅退出与重启恢复
- 每 job 独立 git worktree；修复-验证循环有界（默认最多 3 次，超限 escalate）
- **多仓库策略**：`<repo>/.mewcode/policy.yaml` 声明触发严重度路由 / 目标分支 / 本仓 token 预算 / 通知渠道（仓库级 > 服务配置 > 默认；按 job 现读、改文件即生效；损坏的策略快速失败而非静默回退）
- PR body 由服务层结构化拼装，review 者不看 agent 日志也能做判断
- **运营端点**：`/metrics`（Prometheus 文本：状态/仓库计数、alert→PR 直方图、token 成本）、`/jobs/{id}/report`（单 job JSON 复盘）、`/costs`（按仓库聚合 token，只报数量不换算金额）

**无人值守的信任层**
- **Docker 沙箱**：agent 在容器内执行（非 root、`--cap-drop=ALL`、`no-new-privileges`、只读根文件系统、CPU/内存/PID 限额、硬超时强杀）；依赖按锁文件钉版构建，镜像 tag 内容寻址
- **内部工具链只读**：内置两个 MCP server——生产日志查询（Loki）与 CI 状态（GitHub），整条取数路径只有 GET；未声明 `readOnlyHint` 的工具在服务模式一律不挂
- **自起测试环境**：仓库带 `docker-compose.yml` 时，服务自动 `up -d --wait` 起依赖，在沙箱容器内（加入 compose 网络、按服务名寻址）跑集成测试，无论成败 `down -v` 清理；无容器运行时则如实记录"未执行"，不用假验证充数

## 快速开始（CLI / TUI）

要求：Python 3.11+、[uv](https://docs.astral.sh/uv/)。

```bash
git clone https://github.com/pretextQ/MewCode.git
cd MewCode
uv sync
uv run mewcode        # 交互式 TUI
```

### 配置 LLM 提供商

编辑 `.mewcode/config.yaml`：

```yaml
providers:
  - name: deepseek-anthropic
    protocol: anthropic                          # 支持 anthropic / openai / openai-compat
    base_url: https://your-api-provider.com/api/anthropic
    # api_key 建议留空，走环境变量（anthropic -> ANTHROPIC_API_KEY，
    # openai/openai-compat -> OPENAI_API_KEY）；也支持 ${VAR} 形式引用
    model: claude-sonnet-4-6
    thinking: true                               # extended thinking

mcp_servers:
  # stdio 模式（transport 缺省为 stdio）
  - name: context7
    command: npx
    args: ["-y", "@upstash/context7-mcp"]
  # Streamable HTTP 模式
  # - name: my-http-mcp
  #   url: https://example.com/mcp
  #   transport: http
  #   headers:
  #     Authorization: "Bearer ${MY_TOKEN}"
```

说明：`api_key` 填写字面量会遮蔽环境变量；stdio 用 `command`/`args`，HTTP 用 `url` + `transport: http`（可带 `headers`），二者不可混用。

### 非交互 / headless 调用

```bash
uv run mewcode -p "fix the failing test in calc.py" --mode dontAsk --output-format json
```

`--output-format json` 输出机器可读摘要（result / usage / toolCalls），供上层系统消费。

## 告警驱动的服务模式

### 1. 配置 `service:` 段

```yaml
service:
  port: 9300                   # 默认 8321；Windows 保留了部分端口段，不可绑定时换端口
  token_budget: 500000         # 单 job token 预算熔断（0 = 不限）
  vcs:
    provider: github
    base_branch: main
  repos:
    demo:
      path: /path/to/your/repo                  # 本地 checkout（服务在其上建 worktree）
      url: https://github.com/you/your-repo.git
      base_branch: main
      test_command: python test_app.py          # 验证命令（worktree 内执行）
      # 仓库带 docker-compose.yml 时可选：起依赖后跑集成测试
      integration_test_command: python test_integration.py
  sandbox:
    enabled: true               # 无 Docker 时自动回退宿主直跑并记 warning
  mcp_servers:                  # 内部工具链（服务模式只挂只读工具）
    - name: logs
      command: python
      args: ["-m", "mewcode.mcp.servers.logs"]
      description: "Query production application logs (Loki, read-only)."
      env:
        MEWCODE_LOKI_URL: "${MEWCODE_LOKI_URL}"
```

### 2. 启动服务并发送告警

```bash
uv run mewcode serve --port 9300

# 用 demo 脚本模拟 Alertmanager webhook（payload 与真实告警同构）
uv run python scripts/m1_demo.py init --path ./demo-repo --bug null_deref
uv run python scripts/m1_demo.py alert --port 9300 --repo demo --bug null_deref
```

之后只需观察：job 依次经过 `received → triaging → reproducing → fixing → verifying → pr_opened → ci_gate → human_review`，PR 出现在仓库里，CI 结论自动写回审计。每次状态转移都落库（SQLite），既是状态机也是审计日志。

失败路径同样是设计的一部分：信息不足的告警在 triaging 即 escalate（不硬修）、agent 无改动收敛为 `cant_repro`、验证/CI 失败带输出重试至上限、重复告警在窗口内去重合并、服务重启后未完结 job 自动恢复（等人工的 job 不会被重复重跑）。

### 3. 多仓库策略（可选）

每个仓库可以在自己的 checkout 里放一份 `<repo>/.mewcode/policy.yaml`，声明与全服务默认不同的行为（仓库级 > 服务配置 > 默认；按 job 现读，改文件即生效、无需重启；损坏的策略一律快速失败——intake 拒收给原因、执行链 escalate，绝不静默回退）：

```yaml
# <repo>/.mewcode/policy.yaml
triggers:
  severities: [critical]   # 触发路由：清单外的严重度在 intake 拒收（422 + 原因）
target_branch: develop     # worktree 基线与 PR base 同时走它
token_budget: 50000        # 本仓预算（直跑中途熔断 + 沙箱事后门禁）
notify:
  type: slack              # 本仓通知渠道（覆盖服务级）
  webhook_url: ${SLACK_WEBHOOK}
```

## 运营化：指标、复盘与评估集回放

服务暴露三个只读端点（与 healthz 同口径，无需 token）：

| 端点 | 内容 |
|---|---|
| `GET /metrics` | Prometheus 文本格式：jobs 总量（按状态/仓库）、`mewcode_alert_to_pr_seconds` 直方图（MTTR 核心）、merged/escalate 计数、按仓库的 token 成本 |
| `GET /jobs/{id}/report` | 单 job JSON 复盘：状态轨迹、逐 attempt token、工具统计、验证证据、MCP 调用、升级原因 |
| `GET /costs?repo=` | 按仓库聚合的 token 成本（诚实口径：只报 provider 原始数量，不换算金额） |

**评估集回放**——改了提示词 / skill / 内核之后跑一轮，就能拿到成功率 / MTTR / 成本的前后对比：

```bash
uv run mewcode serve --port 9300

# 基线：三类真机 bug 用例（config_error / null_deref / unhandled_timeout）批量回放
uv run python scripts/m3_replay.py --label baseline --fingerprint-prefix replay-1 \
    --repo-path /path/to/demo-repo

# 改一处 skill 规则后重跑：必须换 fingerprint 前缀（服务端 1800s 去重窗口），带 --compare 出 A/B 对比
uv run python scripts/m3_replay.py --label after --fingerprint-prefix replay-2 \
    --repo-path /path/to/demo-repo --compare docs/evolution/replay/baseline.json
```

脚本在每单告警发送前先把 demo 仓重置到该 bug 的现场并推 main（保证 PR diff 恰好是"bug→修复"），轮询 job 到 `human_review`/终态，再拉取 PR diff 统计规范标记（默认找 `# fix(...)` 追溯注释，可换正则），输出 JSON+Markdown 报告。

2026-10-05 真机实测（3 用例 × 前后两轮，证据为 [PR #12–#17](https://github.com/pretextQ/mewcode-alert-demo/pulls)，CI 全绿）：

| 指标 | 基线 | org-code-style 加"可追溯注释"规则后 |
|---|---|---|
| 修复 PR 成功率 | 3/3 | 3/3 |
| alert→PR 中位 / p90 | 25s / 29s | 26s / 43s |
| token 总量（3 单） | 27,159 | 27,257 |
| 规范标记命中（PR diff） | 0/3 | **3/3** |

诚实口径：每轮 n=3；p90 波动来自单样本（19s→43s），不解读为趋势；token 为 provider 原始计数。

## 信任模型（无人值守为什么可信）

| 层 | 机制 |
|---|---|
| 应用层 | 权限管线：危险命令检测 → 路径沙箱 → 规则引擎（含仓库级 `permissions.yaml`）；headless 模式 `dontAsk`——"要问人"的动作一律拒绝而非放行 |
| OS 层 | 每 job 容器：非 root、全部能力丢弃、禁止提权、只读根 + tmpfs、CPU/内存/PID 限额、硬超时强杀；只挂载该 job 的 worktree，宿主配置与密钥不进容器（LLM key 只经环境变量白名单注入） |
| 外部系统 | 内部工具链只读（`readOnlyHint` 过滤 + 只 GET）；服务无 merge 权限，PR 是唯一出口，人是唯一门禁 |
| 证据 | 全程事件落库；PR body 携带告警摘要 / 根因 / 修复 / 测试前后对比 / 集成验证 / 审计时间线 |

已知边界：沙箱网络当前提供 `bridge`（可出网，agent 需访问 LLM API）与 `none`（完全隔离）两档；"只放行 LLM API 与 git 远端"的细粒度 egress 白名单需要宿主侧防火墙规则（Linux 环境），尚未内置——生产部署需自行在网络层收紧。

## 项目结构

```
mewcode/
├── agent.py            # agent 主循环（工具调度 / 上下文管理 / 事件）
├── tools/              # 内置工具集（读写编辑/Bash/Glob/Grep/子代理…）
├── permissions/        # 权限管线（危险命令 / 路径沙箱 / 规则引擎 / 模式）
├── mcp/                # MCP 客户端与内置 server（logs 接 Loki / ci 接 GitHub）
├── skills/             # Skill 加载与内置规范包
├── worktree/           # git worktree 管理（创建 / 快速恢复 / 陈旧清理）
├── teams/ agents/      # 多 agent 协作（子代理 / 团队）
└── service/            # 无头服务层
    ├── api.py          #   webhook / healthz / jobs 查询
    ├── triggers/       #   Alertmanager / 手动触发适配
    ├── jobs.py         #   SQLite JobStore + 状态机 + 去重
    ├── worker.py       #   有界并发消费池（超时收敛 / 优雅退出 / 恢复）
    ├── execution.py    #   执行链（worktree → agent → 验证 → 发布）
    ├── sandbox.py      #   Docker 沙箱执行器
    ├── compose.py      #   自起测试环境（compose 依赖 + 容器内集成测试）
    ├── publisher.py    #   PR 证据链拼装 + CI 门禁
    ├── vcs.py          #   GitHub REST（branch / push / PR / checks）
    ├── notify.py       #   钉钉 / 企微 / Slack webhook 通知
    ├── metrics.py      #   /metrics + /jobs/{id}/report + /costs（M3）
    ├── policy.py       #   仓库级策略文件解析与四生效点（M3）
    └── ...
scripts/
├── m1_demo.py          # demo 仓库构建 + 模拟告警发送
├── m1_github_setup.py  # demo 仓库一键建仓
└── m3_replay.py        # 评估集回放：批量告警 → 报告（成功率/MTTR/token）+ A/B 对比
```

## 测试

```bash
uv run pytest                                          # 全量套件（1180+ 用例）
MEWCODE_DOCKER_TESTS=1 uv run pytest tests/test_sandbox_live.py   # 真机容器测试（需 Docker）
```

- 默认套件完全脱网：agent 用测试替身（假 LLM / 假后端），执行链用真实 git worktree + 真实子进程
- 真机套件验证的是隔离本身：非 root、只读根、宿主文件不可见、断网生效、限额生效、容器无残留、容器内完整 agent 路径（含 MCP）、compose 起停与清理

## 平台说明

Windows 是一等公民运行平台，已知边界（GBK 编码读取、行尾保持、进程树清理等）
见 [guides/windows-platform-notes.md](guides/windows-platform-notes.md)。
权限分层语义见 [guides/permission-semantics.md](guides/permission-semantics.md)，
hook stdin JSON 契约见 [guides/hook-contract.md](guides/hook-contract.md)。
