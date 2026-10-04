"""M2 W3：内置只读 MCP server（内部工具链）。

无人值守的 agent 没有同事可以问，也进不了内网门户——它的"内部工具"就是
这些 MCP server。先只上两个只读服务（日志查询 + CI 状态），写权限一律不给：
在能自动改代码的系统里，agent 对外的写权限是最危险的一类权限。

两个 server 都是 stdio 实现，因此既能被 TUI/服务直跑连接，也能在沙箱容器
内启动（源码只读挂载 + 依赖预装在镜像里，见 service/sandbox.py）。

    python -m mewcode.mcp.servers.logs   # 日志查询（Loki）
    python -m mewcode.mcp.servers.ci     # CI 状态（GitHub）
"""
