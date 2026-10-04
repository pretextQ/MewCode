# MewCode

MewCode 是一个 AI Coding Agent 的完整实现，帮助你理解 AI Agent 的核心原理与工程实践。

## 环境要求

- Python 3.11+
- [uv](https://docs.astral.sh/uv/)

## 快速开始

### 1. 配置

编辑 `.mewcode/config.yaml`，填入你的 LLM 提供商信息：

```yaml
providers:
  - name: deepseek-anthropic
    protocol: anthropic                          # 支持 anthropic / openai / openai-compat 三种协议
    base_url: https://your-api-provider.com/api/anthropic
    # api_key 建议留空，走环境变量（anthropic -> ANTHROPIC_API_KEY，
    # openai/openai-compat -> OPENAI_API_KEY）；也可以填 "YOUR_API_KEY"
    # 或支持 ${VAR} 形式引用环境变量
    model: claude-sonnet-4-6
    thinking: true                               # 是否开启 extended thinking

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

**说明：**
- `protocol`：填 `anthropic` / `openai` / `openai-compat`，取决于你的提供商兼容哪种 API
- `base_url`：你的 API 地址
- `api_key`：建议留空走环境变量回退；填写字面量会遮蔽环境变量，支持 `${VAR}` 展开
- `model`：模型名称
- `mcp_servers`：MCP Server 列表。stdio 模式用 `command`/`args`（`transport: stdio` 缺省）；
  HTTP 模式用 `url` + `transport: http`，可带 `headers`；二者不可混用

### 2. 安装 & 运行

```bash
uv sync
uv run mewcode
```

### 3. 运行测试

```bash
uv run pytest
```

### 4. 代码质量检查

```bash
uv run ruff check .
uv run mypy mewcode/tools mewcode/permissions mewcode/serialization.py
```

## 平台说明

Windows 是一等公民运行平台，已知边界（GBK 编码读取、行尾保持、进程树清理等）
见 [guides/windows-platform-notes.md](guides/windows-platform-notes.md)。
权限分层语义见 [guides/permission-semantics.md](guides/permission-semantics.md)，
hook stdin JSON 契约见 [guides/hook-contract.md](guides/hook-contract.md)。
