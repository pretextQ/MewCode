"""权限与安全回归测试套件。

阶段 1 起逐项累积，覆盖审查报告 §3.1–3.8 的全部绕过示例（F4.4 汇总挂 CI）。
执行纪律：先写"绕过复现"测试（红），修复后转绿。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, AsyncIterator

import pytest
from pydantic import BaseModel

from mewcode.agent import (
    Agent,
    PermissionRequest,
    PermissionResponse,
    ToolResultEvent,
)
from mewcode.client import LLMClient
from mewcode.conversation import ConversationManager
from mewcode.hooks import Action, Hook, HookEngine
from mewcode.permissions import (
    DangerousCommandDetector,
    PathSandbox,
    PermissionChecker,
    PermissionMode,
    RuleEngine,
)
from mewcode.tools import create_default_registry
from mewcode.tools.base import (
    StreamEnd,
    TextDelta,
    Tool,
    ToolCallComplete,
    ToolResult,
)


# ---------------------------------------------------------------------------
# 测试基建
# ---------------------------------------------------------------------------

class MockLLMClient(LLMClient):
    def __init__(self, responses: list[list[Any]]) -> None:
        self._responses = list(responses)
        self._call_index = 0

    async def stream(
        self,
        conversation: ConversationManager,
        system: str = "",
        tools: list[dict[str, Any]] | None = None,
    ) -> AsyncIterator[Any]:
        if self._call_index >= len(self._responses):
            yield TextDelta(text="No more responses")
            yield StreamEnd(stop_reason="end_turn", input_tokens=1, output_tokens=1)
            return
        for e in self._responses[self._call_index]:
            yield e
        self._call_index += 1


class _DummyWriteParams(BaseModel):
    target: str = ""


class DummyWriteTool(Tool):
    """write 类别且并发安全的测试工具：DEFAULT 模式下应触发 ask。"""

    name = "DummyWrite"
    description = "test write tool"
    params_model = _DummyWriteParams
    category = "write"
    is_concurrency_safe = True

    async def execute(self, params: BaseModel) -> ToolResult:
        return ToolResult(output=f"wrote {getattr(params, 'target', '')}")


class _BrokenParams(BaseModel):
    pass


class BrokenTool(Tool):
    """execute 返回 None，触发 _snapshot_for_recovery 的逃逸异常。"""

    name = "Broken"
    description = "test tool that returns None"
    params_model = _BrokenParams
    category = "read"
    is_concurrency_safe = True

    async def execute(self, params: BaseModel) -> ToolResult:
        return None  # type: ignore[return-value]


def _make_agent(tmp_path: Path, client: LLMClient, **kwargs: Any) -> Agent:
    registry = create_default_registry()
    checker = PermissionChecker(
        detector=DangerousCommandDetector(),
        sandbox=PathSandbox(str(tmp_path)),
        rule_engine=RuleEngine(local_rules_path=tmp_path / "local_rules.yaml"),
        mode=PermissionMode.DEFAULT,
    )
    kwargs.setdefault("permission_checker", checker)
    return Agent(client, registry, "anthropic", work_dir=str(tmp_path), **kwargs)


def _read_call(tool_id: str, path: Path) -> ToolCallComplete:
    return ToolCallComplete(tool_id, "ReadFile", {"file_path": str(path)})


# ---------------------------------------------------------------------------
# F1.1 并行批次的权限门禁
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_parallel_batch_respects_deny_rule(tmp_path: Path):
    """绕过示例：并行批次的 ReadFile 不经权限检查（§3.1）。

    deny 规则命中的工具必须返回 error result，而不是文件内容。
    """
    (tmp_path / "normal.txt").write_text("ok-content")
    (tmp_path / "secret.txt").write_text("hidden-content")
    rules = tmp_path / "local_rules.yaml"
    rules.write_text(
        "- rule: 'ReadFile(*secret.txt)'\n  effect: deny\n", encoding="utf-8"
    )

    client = MockLLMClient([
        [
            _read_call("t1", tmp_path / "normal.txt"),
            _read_call("t2", tmp_path / "secret.txt"),
            StreamEnd("end_turn", input_tokens=10, output_tokens=20),
        ],
        [TextDelta("done."), StreamEnd("end_turn", input_tokens=5, output_tokens=5)],
    ])
    agent = _make_agent(tmp_path, client)
    conv = ConversationManager()
    conv.add_user_message("read both files")

    results: dict[str, ToolResultEvent] = {}
    async for e in agent.run(conv):
        if isinstance(e, ToolResultEvent):
            results[e.tool_id] = e

    assert "t1" in results and "t2" in results
    assert not results["t1"].is_error
    assert results["t2"].is_error
    assert "Permission denied" in results["t2"].output
    assert "hidden-content" not in results["t2"].output


@pytest.mark.asyncio
async def test_parallel_batch_ask_escalates_permission_request(tmp_path: Path):
    """绕过示例：并行批次中触发 ask 的工具直接执行，无人工确认（§3.1）。"""
    (tmp_path / "normal.txt").write_text("ok-content")

    client = MockLLMClient([
        [
            _read_call("t1", tmp_path / "normal.txt"),
            ToolCallComplete("t2", "DummyWrite", {"target": "x"}),
            StreamEnd("end_turn", input_tokens=10, output_tokens=20),
        ],
        [TextDelta("done."), StreamEnd("end_turn", input_tokens=5, output_tokens=5)],
    ])
    agent = _make_agent(tmp_path, client)
    agent.registry.register(DummyWriteTool())
    conv = ConversationManager()
    conv.add_user_message("read file and write")

    permission_requests: list[PermissionRequest] = []
    results: dict[str, ToolResultEvent] = {}
    async for e in agent.run(conv):
        if isinstance(e, PermissionRequest):
            permission_requests.append(e)
            e.future.set_result(PermissionResponse.ALLOW)
        elif isinstance(e, ToolResultEvent):
            results[e.tool_id] = e

    assert len(permission_requests) == 1
    assert permission_requests[0].tool_name == "DummyWrite"
    assert "t2" in results
    assert not results["t2"].is_error
    assert "wrote x" in results["t2"].output


@pytest.mark.asyncio
async def test_parallel_batch_pre_hook_reject(tmp_path: Path):
    """绕过示例：并行批次绕过 pre_tool_use hooks（§3.1）。"""
    (tmp_path / "a.txt").write_text("a")
    (tmp_path / "b.txt").write_text("b")
    hook = Hook(
        id="h1",
        event="pre_tool_use",
        action=Action(type="command", command="echo blocked-by-hook"),
        reject=True,
    )
    client = MockLLMClient([
        [
            _read_call("t1", tmp_path / "a.txt"),
            _read_call("t2", tmp_path / "b.txt"),
            StreamEnd("end_turn", input_tokens=10, output_tokens=20),
        ],
        [TextDelta("done."), StreamEnd("end_turn", input_tokens=5, output_tokens=5)],
    ])
    agent = _make_agent(tmp_path, client, hook_engine=HookEngine([hook]))
    conv = ConversationManager()
    conv.add_user_message("read both")

    results: dict[str, ToolResultEvent] = {}
    async for e in agent.run(conv):
        if isinstance(e, ToolResultEvent):
            results[e.tool_id] = e

    assert "t1" in results and "t2" in results
    for tid in ("t1", "t2"):
        assert results[tid].is_error, f"{tid} should be rejected by hook"
        assert "Hook rejected" in results[tid].output
        assert "blocked-by-hook" in results[tid].output
        assert (tmp_path / ("a.txt" if tid == "t1" else "b.txt")).exists()


@pytest.mark.asyncio
async def test_parallel_batch_exception_isolation(tmp_path: Path):
    """绕过示例：并行批次 gather 无 return_exceptions，一个工具的逃逸异常炸掉整个循环（§3.1/P1）。"""
    (tmp_path / "normal.txt").write_text("ok-content")

    client = MockLLMClient([
        [
            _read_call("t1", tmp_path / "normal.txt"),
            ToolCallComplete("t2", "Broken", {}),
            StreamEnd("end_turn", input_tokens=10, output_tokens=20),
        ],
        [TextDelta("done."), StreamEnd("end_turn", input_tokens=5, output_tokens=5)],
    ])
    agent = _make_agent(tmp_path, client)
    agent.registry.register(BrokenTool())
    conv = ConversationManager()
    conv.add_user_message("read file and broken call")

    results: dict[str, ToolResultEvent] = {}
    async for e in agent.run(conv):  # 不应抛出异常
        if isinstance(e, ToolResultEvent):
            results[e.tool_id] = e

    assert "t1" in results
    assert not results["t1"].is_error
    assert "t2" in results
    assert results["t2"].is_error
    assert "Tool execution error" in results["t2"].output


# ---------------------------------------------------------------------------
# F1.2 权限管线重排：危险检测前移 + 白名单清理 + 禁用字符表
# ---------------------------------------------------------------------------

class TestSafeCommandWhitelist:
    """审查报告绕过示例：白名单里的可写/可执行命令必须不再自动放行。"""

    @pytest.mark.parametrize("command", [
        'find . -name "*.py" -delete',
        "sed -i 's/x/y/' C:/Users/x/f.txt",
        "npx some-pkg",
        'awk \'BEGIN{system("evil")}\'',
    ])
    def test_writey_commands_not_safe(self, command: str):
        from mewcode.permissions.dangerous import is_safe_command
        assert not is_safe_command(command), command

    @pytest.mark.parametrize("command", [
        "echo hi\nrm -rf ~",
        "ls & rm -rf x",
        "cat /etc/passwd",
        "cat ~/.ssh/id_rsa",
        "cat C:/Users/x/.ssh/id_rsa",
    ])
    def test_injection_and_absolute_path_not_safe(self, command: str):
        from mewcode.permissions.dangerous import is_safe_command
        assert not is_safe_command(command), command

    @pytest.mark.parametrize("command", [
        "ls",
        "pwd",
        "git status",
        "git log --oneline",
        "cat README.md",
        "python --version",
    ])
    def test_benign_commands_still_safe(self, command: str):
        from mewcode.permissions.dangerous import is_safe_command
        assert is_safe_command(command), command


@pytest.mark.asyncio
async def test_dangerous_check_precedes_safe_allow(tmp_path: Path):
    """Layer 1b（危险黑名单）必须先于 Layer 1（安全白名单）生效。

    用 extra_patterns 让 ls 命中黑名单：旧顺序下白名单先 return，黑名单不可达。
    """
    from mewcode.tools.bash import Bash

    detector = DangerousCommandDetector(extra_patterns=[(r"^ls", "unit-test deny")])
    checker = PermissionChecker(
        detector=detector,
        sandbox=PathSandbox(str(tmp_path)),
        rule_engine=RuleEngine(),
        mode=PermissionMode.DEFAULT,
    )
    d = checker.check(Bash(), {"command": "ls"})
    assert d.effect == "deny"
