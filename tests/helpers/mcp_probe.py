"""容器内的 MCP 探针：用与 agent 完全相同的接线路径连内部工具并调用一次。

沙箱真机测试用它验证"内置 MCP server 能在容器里跑起来、能取到数据"——
不需要 LLM，也不需要宿主网络：假后端在同一个容器里起。

    python mcp_probe.py --config-file /workspace/mcp.json \
        --tool query_logs --tool-args-file /workspace/args.json

输出一行 JSON：{"tools": [...], "output": "...", "is_error": false, "errors": [...]}
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path


def _load_json(inline: str, path: str) -> dict:
    if path:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    return json.loads(inline or "{}")


async def _run(args: argparse.Namespace) -> int:
    from mewcode.config import MCPServerConfig
    from mewcode.mcp.bootstrap import close_mcp, register_mcp_tools
    from mewcode.tools import ToolRegistry

    raw = _load_json(args.config, args.config_file)
    server = MCPServerConfig(
        name=raw.get("name", "probe"),
        command=raw.get("command"),
        args=list(raw.get("args") or []),
        url=raw.get("url"),
        env=dict(raw.get("env") or {}),
        transport=raw.get("transport", "stdio"),
    )
    registry = ToolRegistry()
    result = await register_mcp_tools(registry, [server])
    payload: dict = {
        "tools": result.tool_names,
        "errors": result.errors,
        "output": "",
        "is_error": True,
    }
    try:
        # 允许短名（query_logs）或注册全名（mcp_logs_query_logs）
        tool_name = args.tool or (result.tool_names[0] if result.tool_names else "")
        tool = registry.get(tool_name)
        if tool is None and tool_name:
            matches = [n for n in result.tool_names if n.endswith(f"_{tool_name}")]
            if len(matches) == 1:
                tool_name = matches[0]
                tool = registry.get(tool_name)
        if tool is None:
            payload["output"] = f"tool {tool_name!r} not registered"
        else:
            params = tool.params_model(**_load_json(args.tool_args, args.tool_args_file))
            outcome = await tool.execute(params)
            payload["output"] = outcome.output
            payload["is_error"] = outcome.is_error
    except Exception as e:  # 探针本身出错也要留下可诊断的输出
        payload["output"] = f"probe error: {type(e).__name__}: {e}"
    finally:
        await close_mcp(result)
    # 无论成功失败都打印：静默失败会让真机排障全靠猜（这里踩过一次）
    print(json.dumps(payload, ensure_ascii=False), flush=True)
    return 0 if payload["output"] and not payload["is_error"] else 2


def main() -> None:
    parser = argparse.ArgumentParser(description="probe an MCP server through the headless bootstrap")
    parser.add_argument("--config", default="", help="MCP server config as JSON")
    parser.add_argument("--config-file", default="", help="path to the MCP server config JSON")
    parser.add_argument("--tool", default="", help="tool name to call (default: first registered)")
    parser.add_argument("--tool-args", default="{}", help="tool arguments as JSON")
    parser.add_argument("--tool-args-file", default="", help="path to the tool arguments JSON")
    sys.exit(asyncio.run(_run(parser.parse_args())))


if __name__ == "__main__":
    main()

