# MCP 加载与兼容分发

默认仍为 `legacy`。TUI 保留原有 ToolSearch 发现后加入 tools 的行为；headless 和服务默认仍直接暴露已通过 `readOnlyHint` 过滤的工具。不引入 native/tool_reference、beta header 或新的 MCP SDK API。

可在配置顶层设置 `mcp_loading`（TUI / `-p`），或独立设置 `service.mcp_loading`（服务直跑及容器）。只接受下列四种值，配置层可显式覆盖回 `legacy`：

| 值 | 行为 |
| --- | --- |
| `legacy` | 各入口的原有可见性策略 |
| `eager` | 初次连接完成后，全部已注册且启用的 MCP schema 可见 |
| `dispatch` | MCP schema 不加入请求 tools；ToolSearch 返回完整 schema，MCPCall 调用真实目标 |
| `auto` | 显式启用；启动时按 schema 估算选择 eager 或 dispatch，之后固定 |

`auto` 将已启用 MCP schema 列表以紧凑 JSON 编码为 UTF-8，字节数除以 4 向上取整作为估计 token。阈值为 `min(8192, max(1, context_window // 10))`；大于阈值选 dispatch，否则 eager。它不是端点 tokenizer 或账单测量。空集合不安装多余 MCPCall。模式在初次连接结束后选择，不因每轮搜索或上下文增长重新选择。

例如，仅启用 TUI/headless 的 dispatch，服务保持原默认：

```yaml
mcp_loading: dispatch
service:
  mcp_loading: legacy
```

完整 provider/server 配置仍使用已有格式。切换模式后重新启动相应会话；不静默回退到权限更宽的模式。

在 dispatch 中，先调用 `ToolSearch`（关键字或 `select:mcp_<server>_<tool>`），再调用：

```json
{"tool_name":"mcp_logs_query_logs","arguments":{"query":"{app=\"checkout\"}"}}
```

以上是 MCPCall 的参数。工具 schema 保持原注册名称与完整 JSON Schema，搜索后不追加这些 schema 到请求 tools。非 MCP 的延迟工具仍按原机制发现。工具输出仍遵守共享上下文结果预算；过大的搜索结果可能由原有结果持久化/截断机制处理，不能保证任意大的 schema 在单次上下文内完整展开。

MCPCall 只是协议入口，不能自行直调目标。Agent 在执行前按当前注册表解析真实 MCPToolWrapper，随后复用目标的启停、角色 allowlist、PermissionChecker、参数验证、pre/post hooks、监督与只读策略。权限规则继续写真实名称（如 `mcp_logs_query_logs(*)`），hooks 和执行监督也只记真实目标一次；不会先执行外层 hooks 再重复记内层。原 assistant tool_use 和外层结果继续使用原 tool_call ID。流式开始事件仍表示模型提出的 MCPCall，解析后的完成事件与无人值守执行计数使用真实目标名称。

未知、禁用、被角色/PLAN/服务过滤、不可用、非 MCP 以及递归目标均不进入实际调用。MCPCall 和 dispatch 目标保守串行；其他普通工具的既有行为保留。隐藏 schema 是加载策略，权限仍由真实目标门禁执行，不能将 tools 列表当权限边界。

复制/绑定注册表时，ToolSearch 和 MCPCall 都重新绑定到目标注册表，不能保留父级能力；禁用、发现和碰撞状态也继承。注册名称碰撞明确报错，并封锁歧义身份，enable_all 不会解除碰撞。dispatcher 名称碰撞同样封锁本批 MCP 目标。

完整 JSON Schema 在本地验证，拒绝错误类型、范围、嵌套约束、未知字段（schema 禁止时）与外部 `$ref`。不拆逗号字符串、不隐式转换数字；合法额外字段、显式 null 和嵌套数据原样发送。未提供的可选字段不伪造为 null。

连接重建有递增代际；每个目标首次在新连接执行前重新核对远端定义。定义变化或消失时拒绝调用、撤销该目标可见性/启用状态，提示重启会话建立新集合；刷新失败也拒绝调用。重连先关闭旧连接栈，保留进程收尾。没有订阅或宣称支持远端全部动态能力通知。稳定性仅适用于工具集合、权限、连接代际和模式没有实际变更的会话段；服务模式始终不能分发至剔除的写工具。

离线合成样本（0/5/250 工具，schema 总 UTF-8 字节 2/1831/91501）：

| 工具数 | legacy tools 字节：搜索前→后 | eager | dispatch |
| --- | --- | --- | --- |
| 0 | 404→404 | 404→404 | 404→404 |
| 5 | 404→2234 | 2234→2234 | 799→799 |
| 250 | 404→91904 | 91904→91904 | 799→799 |

该样本只含 ToolSearch 与合成 MCP 工具，用紧凑 JSON 序列化。真实三客户端 SDK 捕获、本地 stdio、离线 TUI/headless/service 与容器完整链路见[交付台账](../docs/acceptance/stage-2026-11-02-borrowing.md)。没有真实模型调用、缓存命中、费用或模型遵循效果结论。
