"""M2 W3：内部工具链接线测试（配置 → 提示词 → 执行链 → 容器 → PR body）。

覆盖的是"接线"而不是 server 本身（后者在 test_mcp_servers.py）：
配置能被解析、直跑模式真的注册了只读工具、容器配置带着 server 且密钥
只走环境变量、提示词/PR body 里能看到内部工具的存在与使用。
"""
from __future__ import annotations

import subprocess
import sys
import textwrap
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest

from mewcode.config import ConfigError, MCPServerConfig, ProviderConfig, RepoConfig, SandboxConfig, ServiceConfig, load_config
from mewcode.service.execution import AgentRunOutcome, ExecutionChain, HeadlessAgentRunner
from mewcode.service.jobs import JobStore
from mewcode.service.sandbox import DockerSandbox, mcp_env_passthrough
from mewcode.tools import ToolRegistry

LOGS_SERVER = MCPServerConfig(
    name="logs",
    command=sys.executable,
    args=["-m", "mewcode.mcp.servers.logs"],
    env={"MEWCODE_LOKI_URL": "http://loki.internal:3100"},
    description="Query production logs (Loki).",
)


# =========================================================================
# A. 配置解析：service.mcp_servers
# =========================================================================

class TestServiceConfigParsing:
    def _write(self, tmp_path: Path, body: str) -> Path:
        path = tmp_path / "config.yaml"
        path.write_text(textwrap.dedent(body), encoding="utf-8")
        return path

    def test_service_mcp_servers_are_parsed_with_description(self, tmp_path: Path) -> None:
        path = self._write(tmp_path, """\
            providers:
              - name: test
                protocol: openai
                base_url: http://localhost
                model: gpt-4o
            service:
              mcp_servers:
                - name: logs
                  command: ["python"]
                  args: ["-m", "mewcode.mcp.servers.logs"]
                  description: "Query production logs (Loki)."
                  env:
                    MEWCODE_LOKI_URL: "${LOKI_URL}"
        """)
        config = load_config(path)
        assert len(config.service.mcp_servers) == 1
        server = config.service.mcp_servers[0]
        assert server.name == "logs"
        assert server.description == "Query production logs (Loki)."
        assert server.env == {"MEWCODE_LOKI_URL": "${LOKI_URL}"}
        # 顶层 mcp_servers（CLI/TUI 用）不受影响
        assert config.mcp_servers == []

    def test_default_is_empty(self, tmp_path: Path) -> None:
        path = self._write(tmp_path, """\
            providers:
              - name: test
                protocol: openai
                base_url: http://localhost
                model: gpt-4o
            service:
              port: 9300
        """)
        assert load_config(path).service.mcp_servers == []

    def test_invalid_entry_is_rejected(self, tmp_path: Path) -> None:
        path = self._write(tmp_path, """\
            providers:
              - name: test
                protocol: openai
                base_url: http://localhost
                model: gpt-4o
            service:
              mcp_servers:
                - name: broken
                  env: {FOO: bar}
        """)
        with pytest.raises(ConfigError, match="must have either"):
            load_config(path)


# =========================================================================
# B. 沙箱：容器配置带上 MCP，密钥只走环境变量
# =========================================================================

PROVIDER = ProviderConfig(name="t", protocol="openai", base_url="http://llm:1/v1", model="m", api_key="k")


def _sandbox(tmp_path: Path, **kwargs) -> DockerSandbox:
    return DockerSandbox(
        SandboxConfig(**kwargs),
        mewcode_src=str(tmp_path / "src"),
        work_root=str(tmp_path / "work"),
    )


class TestSandboxContainerMCP:
    def test_container_config_carries_mcp_servers_with_placeholders(self, tmp_path: Path) -> None:
        import yaml

        sandbox = _sandbox(tmp_path)
        secret_server = MCPServerConfig(
            name="ci",
            command="python",
            args=["-m", "mewcode.mcp.servers.ci"],
            env={"GITHUB_TOKEN": "${GITHUB_TOKEN}"},
            description="Check CI status.",
        )
        config_path = sandbox.write_container_config(
            tmp_path / "config.yaml", PROVIDER, mcp_servers=[LOGS_SERVER, secret_server]
        )
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))

        assert [s["name"] for s in raw["mcp_servers"]] == ["logs", "ci"]
        assert raw["mcp_servers"][0]["env"]["MEWCODE_LOKI_URL"] == "http://loki.internal:3100"
        assert raw["mcp_servers"][0]["description"] == "Query production logs (Loki)."
        # 密钥不落配置：只有占位符，真实值在容器环境变量里
        assert raw["mcp_servers"][1]["env"]["GITHUB_TOKEN"] == "${GITHUB_TOKEN}"
        assert raw["providers"][0]["api_key"] == ""

    def test_container_config_without_mcp_has_no_section(self, tmp_path: Path) -> None:
        import yaml

        sandbox = _sandbox(tmp_path)
        config_path = sandbox.write_container_config(tmp_path / "config.yaml", PROVIDER)
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        assert "mcp_servers" not in raw

    def test_referenced_env_vars_are_passed_through(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setenv("GITHUB_TOKEN", "gh-token-value")
        monkeypatch.setenv("LOKI_URL", "http://loki.internal:3100")

        server = MCPServerConfig(
            name="tools",
            command="python",
            env={"GITHUB_TOKEN": "${GITHUB_TOKEN}", "LOKI": "${LOKI_URL}"},
            headers={"X-Extra": "${SOME_OTHER_VAR}"},
        )
        assert mcp_env_passthrough([server]) == {"GITHUB_TOKEN", "LOKI_URL", "SOME_OTHER_VAR"}

        sandbox = _sandbox(tmp_path)
        env = sandbox.container_env(PROVIDER, mcp_servers=[server])
        # 透传的是**被引用的名字**：容器内的 MCP 子进程再按 ${VAR} 解析，
        # 所以容器里存在的是 GITHUB_TOKEN / LOKI_URL 本身
        assert env["GITHUB_TOKEN"] == "gh-token-value"
        assert env["LOKI_URL"] == "http://loki.internal:3100"
        # 引用了但宿主没有的变量不进容器（空值等于没配）
        assert "SOME_OTHER_VAR" not in env

    def test_container_env_targets_are_limited_to_referenced_names(self, tmp_path: Path, monkeypatch) -> None:
        """白名单之外的环境变量（哪怕宿主上有）不进容器。"""
        monkeypatch.setenv("SECRET_NOT_REFERENCED", "leak-me")
        sandbox = _sandbox(tmp_path)
        env = sandbox.container_env(PROVIDER, mcp_servers=[LOGS_SERVER])
        assert "SECRET_NOT_REFERENCED" not in env

    def test_run_agent_forwards_mcp_servers(self, tmp_path: Path) -> None:
        """``run_agent`` 必须把 mcp_servers 交给容器配置与容器环境。"""
        sandbox = _sandbox(tmp_path, keep_containers=True)
        captured: dict[str, Any] = {}

        def fake_build_run_args(**kwargs):
            captured.update(kwargs)
            return ["true"]

        async def fake_available() -> bool:
            return True

        async def fake_ensure_image(repo_name: str, work_dir: str | None = None) -> str:
            return "img"

        async def fake_run(argv, timeout, cwd=None):
            return 0, '{"result": "ok", "usage": {"inputTokens": 1, "outputTokens": 1}, "toolCalls": 0}'

        sandbox.build_run_args = fake_build_run_args  # type: ignore[method-assign]
        sandbox.available = fake_available  # type: ignore[method-assign]
        sandbox.ensure_image = fake_ensure_image  # type: ignore[method-assign]
        sandbox._run = fake_run  # type: ignore[method-assign]

        import asyncio

        result = asyncio.run(
            sandbox.run_agent(
                "job-1", str(tmp_path), "prompt", PROVIDER, timeout=10, mcp_servers=[LOGS_SERVER]
            )
        )
        assert result.result_text == "ok"
        assert captured["config_path"].endswith("config.yaml")
        import yaml

        raw = yaml.safe_load(Path(captured["config_path"]).read_text(encoding="utf-8"))
        assert raw["mcp_servers"][0]["name"] == "logs"

    def test_mount_src_adds_read_only_source_mount(self, tmp_path: Path) -> None:
        sandbox = _sandbox(tmp_path)
        plain = sandbox.build_run_args(
            name="c1", image="img", work_dir=str(tmp_path), shell_command="echo hi"
        )
        with_src = sandbox.build_run_args(
            name="c2", image="img", work_dir=str(tmp_path), shell_command="echo hi", mount_src=True
        )
        assert "PYTHONPATH=/opt/mewcode" not in plain
        assert "PYTHONPATH=/opt/mewcode" in with_src
        assert any(arg.endswith(":/opt/mewcode:ro") for arg in with_src)
        # 只读：源码挂载绝不能是可写的
        assert not any(":/opt/mewcode:rw" in arg for arg in with_src)


# =========================================================================
# C. 直跑模式：runner 注册只读工具、计数、显式收尾
# =========================================================================

class _FakeAgent:
    """带真实注册表的假 agent：MCP 注册发生在它身上（其余行为不需要 LLM）。"""

    def __init__(self, work_dir: str, events: list[dict] | None = None) -> None:
        from mewcode.tools import ToolRegistry

        self.registry = ToolRegistry()
        self.work_dir = work_dir
        self._events = events

    async def run_to_completion(self, prompt, conversation=None, event_callback=None):
        if self._events is not None and event_callback is not None:
            for event in self._events:
                event_callback(event)
        return "ROOT CAUSE: x\nFIX: y\nVERIFICATION: z"

    async def cancel_background_tasks(self) -> None:
        return None


def _runner(config: ServiceConfig) -> HeadlessAgentRunner:
    return HeadlessAgentRunner(config, PROVIDER)


class TestDirectModeMCP:
    def test_offline_server_registers_read_only_tools(self, tmp_path: Path) -> None:
        """即使后端没配（连不上日志平台），工具本身也要注册好并可见。"""
        import asyncio

        config = ServiceConfig(mcp_servers=[LOGS_SERVER])
        runner = _runner(config)
        agent = _FakeAgent(str(tmp_path))
        runner._build_agent = lambda work_dir: agent  # type: ignore[method-assign]

        events: list[dict] = []
        built, result = asyncio.run(
            runner._build_agent_with_tools(str(tmp_path), events.append)
        )

        assert built is agent
        assert sorted(result.tool_names) == ["mcp_logs_list_labels", "mcp_logs_query_logs"]
        assert result.errors == []
        tool = agent.registry.get("mcp_logs_query_logs")
        assert tool.category == "read" and tool.should_defer is False
        assert events and events[0]["type"] == "mcp_ready"

    def test_broken_server_is_reported_but_does_not_raise(self, tmp_path: Path) -> None:
        import asyncio

        broken = MCPServerConfig(name="broken", command="definitely-not-a-real-command-xyz")
        config = ServiceConfig(mcp_servers=[broken])
        runner = _runner(config)
        runner._build_agent = lambda work_dir: _FakeAgent(str(tmp_path))  # type: ignore[method-assign]

        events: list[dict] = []
        _, result = asyncio.run(runner._build_agent_with_tools(str(tmp_path), events.append))

        assert result.tool_names == []
        assert len(result.errors) == 1 and "broken" in result.errors[0]
        assert events[0]["type"] == "mcp_error"

    def test_mcp_usage_is_counted_and_events_recorded(self, tmp_path: Path) -> None:
        import asyncio

        from mewcode.service.jobs import Job

        config = ServiceConfig(mcp_servers=[LOGS_SERVER])
        runner = _runner(config)
        agent = _FakeAgent(str(tmp_path), events=[
            {"type": "tool_use", "toolName": "Read", "args": {"file_path": "a.py"}},
            {"type": "tool_use", "toolName": "mcp_logs_query_logs", "args": {"query": "{app=\"x\"}"}},
            {"type": "usage", "usage": {"inputTokens": 10, "outputTokens": 5}},
        ])
        runner._build_agent = lambda work_dir: agent  # type: ignore[method-assign]

        events: list[dict] = []
        job = Job(id="j1", fingerprint="f", repo="demo", severity="w", status="fixing", payload={})
        outcome = asyncio.run(runner.run(job, str(tmp_path), "prompt", events.append))

        assert outcome.tool_calls == 2
        assert outcome.mcp_tool_calls == 1
        assert outcome.mcp_tools_used == ["mcp_logs_query_logs"]
        assert any(e.get("type") == "mcp_used" for e in events)

    def test_no_mcp_servers_means_no_registry_touch(self, tmp_path: Path) -> None:
        """未配置时完全不碰注册表——零开销、也让老测试的假 agent 继续可用。"""
        import asyncio

        runner = _runner(ServiceConfig())

        class BareAgent:
            async def run_to_completion(self, prompt, conversation=None, event_callback=None):
                return "done"

            async def cancel_background_tasks(self):
                pass

        runner._build_agent = lambda work_dir: BareAgent()  # type: ignore[method-assign]
        from mewcode.service.jobs import Job

        job = Job(id="j2", fingerprint="f", repo="demo", severity="w", status="fixing", payload={})
        outcome = asyncio.run(runner.run(job, str(tmp_path), "prompt", lambda e: None))
        assert outcome.final_text == "done"


# =========================================================================
# D. 提示词：内部工具要出现在首轮指令里
# =========================================================================

class TestPromptInjection:
    def test_internal_tools_section_is_rendered(self) -> None:
        from mewcode.service.jobs import Job
        from mewcode.service.sop import build_alert_prompt

        job = Job(
            id="j1", fingerprint="f", repo="demo", severity="critical",
            status="fixing", payload={"source": "manual", "summary": "s", "logs": "boom"},
        )
        prompt = build_alert_prompt(
            job, "/wt", test_command="pytest",
            mcp_servers=[("logs", "Query production logs (Loki)."), ("ci", "Check CI status.")],
        )
        assert "## Internal tools (read-only MCP servers)" in prompt
        assert "`logs` — Query production logs (Loki)." in prompt
        assert "`ci` — Check CI status." in prompt
        assert "mcp_logs_query_logs" in prompt

    def test_section_absent_without_servers(self) -> None:
        from mewcode.service.jobs import Job
        from mewcode.service.sop import build_alert_prompt

        job = Job(
            id="j1", fingerprint="f", repo="demo", severity="critical",
            status="fixing", payload={"source": "manual", "summary": "s", "logs": "boom"},
        )
        prompt = build_alert_prompt(job, "/wt", test_command="pytest")
        assert "Internal tools" not in prompt

    @pytest.mark.asyncio
    async def test_chain_passes_servers_into_the_prompt(self, tmp_path: Path) -> None:
        repo = _git_repo(tmp_path)
        store = JobStore(tmp_path / "jobs.db")
        await store.connect()
        captured: list[str] = []

        class CapturingRunner:
            async def run(self, job, work_dir, prompt, on_event):
                captured.append(prompt)
                (Path(work_dir) / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
                return AgentRunOutcome(final_text="ROOT CAUSE: x\nFIX: y\nVERIFICATION: z")

        try:
            service = ServiceConfig(
                data_dir=str(tmp_path / "state"),
                repos={"demo": RepoConfig(name="demo", path=str(repo), test_command=_test_command())},
                mcp_servers=[LOGS_SERVER],
            )
            chain = ExecutionChain(service, store, CapturingRunner())
            job = await store.create_job(
                fingerprint="fp-1", repo="demo", severity="critical", title="t",
                payload={"source": "manual", "summary": "add(2,3) wrong", "logs": "AssertionError"},
            )
            await chain(job)
        finally:
            await store.close()

        assert captured, "the runner was never called"
        assert "## Internal tools (read-only MCP servers)" in captured[0]
        assert "`logs`" in captured[0]


# =========================================================================
# E. PR body：内部工具链证据
# =========================================================================

class TestPRBodyEvidence:
    def _context(self, mcp_calls: int = 0, tools: list[str] | None = None):
        from mewcode.service.execution import ExecutionContext

        agent = AgentRunOutcome(
            final_text="ROOT CAUSE: x\nFIX: y",
            tool_calls=3,
            mcp_tool_calls=mcp_calls,
            mcp_tools_used=tools or [],
        )
        return ExecutionContext(
            repo=RepoConfig(name="demo", path="/repo"),
            work_dir="/wt",
            prompt="p",
            agent=agent,
            changed_files=["calc.py"],
            diff="--- a/calc.py\n+++ b/calc.py\n",
            baseline_test=None,
            verify_test=None,
        )

    def _job(self):
        from mewcode.service.jobs import Job

        return Job(
            id="j1", fingerprint="f", repo="demo", severity="critical", status="human_review",
            title="t", payload={"source": "manual", "summary": "s"},
        )

    def test_reports_usage_when_internal_tools_were_used(self) -> None:
        from mewcode.service.publisher import build_pr_body

        body = build_pr_body(
            self._job(), self._context(2, ["mcp_logs_query_logs"]), [],
            mcp_servers=["logs", "ci"],
        )
        assert "## 内部工具链（只读 MCP）" in body
        assert "已配置：`logs`, `ci`" in body
        assert "本次使用：2 次调用（`mcp_logs_query_logs`）" in body

    def test_reports_unused_when_configured_but_unused(self) -> None:
        from mewcode.service.publisher import build_pr_body

        body = build_pr_body(self._job(), self._context(), [], mcp_servers=["logs"])
        assert "本次使用：未使用" in body

    def test_section_absent_when_nothing_configured(self) -> None:
        from mewcode.service.publisher import build_pr_body

        body = build_pr_body(self._job(), self._context(), [], mcp_servers=[])
        assert "内部工具链" not in body


# =========================================================================
# 辅助
# =========================================================================

def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=str(repo), capture_output=True, check=True)


def _git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "demo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "test@test.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "calc.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    (repo / "test_calc.py").write_text(
        "import sys\nfrom calc import add\nsys.exit(0 if add(2, 3) == 5 else 1)\n", encoding="utf-8"
    )
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "init")
    return repo


def _test_command() -> str:
    return f'"{sys.executable}" test_calc.py'
