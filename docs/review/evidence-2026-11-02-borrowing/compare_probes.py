"""Offline comparison probes; use the workspace venv, no user configuration or API calls."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

CHILD = r'''
import asyncio
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import patch

sys.dont_write_bytecode = True
sys.path.insert(0, sys.argv[1])
from mewcode.memory.instructions import load_instructions
from mewcode.permissions import DangerousCommandDetector, PathSandbox, PermissionChecker, PermissionMode, RuleEngine
from mewcode.tools.bash import Bash, Params

base = Path.cwd()
home = base / "home"
root = base / "project"
leaf = root / "packages" / "demo"
leaf.mkdir(parents=True)
(root / ".git").mkdir()
(home / ".mewcode").mkdir(parents=True)
(home / ".mewcode" / "MEWCODE.md").write_text("GLOBAL_INSTRUCTION", encoding="utf-8")
(root / "AGENTS.md").write_text("ROOT_AGENT_INSTRUCTION", encoding="utf-8")
(leaf / "AGENTS.md").write_text("LEAF_AGENT_INSTRUCTION", encoding="utf-8")
(leaf / "MEWCODE.md").write_text("LEAF_MEW_INSTRUCTION", encoding="utf-8")
(leaf / "MEWCODE.local.md").write_text("LOCAL_OVERRIDE_INSTRUCTION", encoding="utf-8")
with patch.object(Path, "home", return_value=home):
    text = load_instructions(str(leaf))
markers = [
    "GLOBAL_INSTRUCTION", "ROOT_AGENT_INSTRUCTION", "LEAF_AGENT_INSTRUCTION",
    "LEAF_MEW_INSTRUCTION", "LOCAL_OVERRIDE_INSTRUCTION",
]
result = {"instructions": {k: k in text for k in markers}}

checker = PermissionChecker(
    DangerousCommandDetector(), PathSandbox(str(root)), RuleEngine(), mode=PermissionMode.BYPASS,
)
result["write_decisions_bypass"] = {
    p: checker.check(
        SimpleNamespace(name="WriteFile", category="write"), {"file_path": str(root / p), "content": "unused"},
    ).effect
    for p in [
        ".mewcode/config.yaml", ".mewcode/permissions.local.yaml",
        ".mewcode/skills/demo/SKILL.md", "src/normal.py",
    ]
}

async def run():
    bash = Bash()
    result["bash_exit_7"] = vars(await bash.execute(Params(command="cmd /c exit 7", timeout=10)))
    predicate = getattr(bash, "is_concurrency_safe", False)
    result["bash_read_concurrent"] = predicate({"command": "git status"}) if callable(predicate) else predicate
    from mcp.types import Tool as MCPTool
    try:
        from mewcode.mcp.tool_wrapper import MCPToolWrapper
    except ImportError as error:
        result["mcp_runtime_unavailable"] = str(error)
        # The strategy module has no runtime MCP dependency; exercise only its
        # endpoint/size decision, without changing or shimming upstream imports.
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "offline_loading_strategy", Path(sys.argv[1]) / "mewcode/mcp/loading_strategy.py",
        )
        strategy = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(strategy)
        os.environ.pop("MEWCODE_MCP_LOADING", None)
        result["mcp_modes"] = {
            "small": strategy.decide_mode("https://example.invalid", 200000, 100).value,
            "large_compat": strategy.decide_mode("https://example.invalid", 200000, 100000).value,
            "large_official": strategy.decide_mode("", 200000, 100000).value,
        }
        return
    from mewcode.tools import ToolRegistry
    from mewcode.tools.impl.tool_search import ToolSearchTool, ToolSearchParams
    registry = ToolRegistry()
    wrapper = MCPToolWrapper(
        "probe",
        MCPTool(name="lookup", description="offline probe", inputSchema={"type": "object", "properties": {}}),
        None,
    )
    registry.register(wrapper)
    try:
        from mewcode.mcp.loading_strategy import apply_mode, McpLoadingMode, decide_mode
    except ImportError:
        pass
    else:
        apply_mode(registry, McpLoadingMode.DISPATCH)
        os.environ.pop("MEWCODE_MCP_LOADING", None)
        result["mcp_modes"] = {
            "small": decide_mode("https://example.invalid", 200000, 100).value,
            "large_compat": decide_mode("https://example.invalid", 200000, 100000).value,
            "large_official": decide_mode("", 200000, 100000).value,
        }
    before = registry.get_all_schemas("anthropic")
    await ToolSearchTool(registry).execute(ToolSearchParams(query="select:" + wrapper.name))
    after = registry.get_all_schemas("anthropic")
    result["mcp_schema_changed_after_search"] = before != after
    result["mcp_schema_counts"] = [len(before), len(after)]

asyncio.run(run())
print(json.dumps(result, ensure_ascii=True))
'''


def main() -> None:
    results = {}
    for label, source in [("local", r"D:\projects\MewCode"), ("reference", r"D:\Downloads\mewcode-python")]:
        with tempfile.TemporaryDirectory(prefix="mewcode-compare-") as work:
            child = subprocess.run(
                [sys.executable, "-B", "-c", CHILD, source], cwd=work,
                env={**os.environ, "PYTHONIOENCODING": "utf-8"},
                capture_output=True, text=True, encoding="utf-8", timeout=30,
            )
            if child.returncode:
                raise RuntimeError(f"{label}: {child.stderr}")
            results[label] = json.loads(child.stdout)
    output = Path(__file__).with_name("observations.json")
    output.write_text(json.dumps(results, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(results, ensure_ascii=True, indent=2))


if __name__ == "__main__":
    main()
