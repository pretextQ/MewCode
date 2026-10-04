"""M2 W1：Docker 沙箱执行器测试。

隔离与限额项是安全属性，必须逐项断言（不能只测"能跑"）。
容器运行时用假的 runtime 可执行文件替代：真实子进程路径被完整走到，
但不需要 daemon，CI 上也能跑；真实 daemon 的端到端见 test_sandbox_docker_live。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from mewcode.config import ProviderConfig, SandboxConfig
from mewcode.service.sandbox import (
    DockerSandbox,
    SandboxUnavailable,
    sandbox_user,
)

FAKE_DOCKER = '''"""假的 docker：记录 argv 并按子命令给出可预期的行为。"""
import json
import os
import sys
import time

record = os.environ["FAKE_DOCKER_RECORD"]
with open(record, "a", encoding="utf-8") as fh:
    fh.write(json.dumps(sys.argv[1:]) + "\\n")

args = sys.argv[1:]
state = os.environ.get("FAKE_DOCKER_STATE", "")
cmd = args[0] if args else ""

def load_state():
    try:
        with open(state, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}

def save_state(data):
    if state:
        with open(state, "w", encoding="utf-8") as fh:
            json.dump(data, fh)

if cmd == "info":
    print("29.4.2")
    sys.exit(0)
if cmd == "image" and args[1:2] == ["inspect"]:
    tag = args[2] if len(args) > 2 else ""
    sys.exit(0 if load_state().get(tag) else 1)
if cmd == "build":
    tag = args[args.index("-t") + 1] if "-t" in args else "?"
    data = load_state(); data[tag] = True; save_state(data)
    print("Successfully built " + tag)
    sys.exit(0)
if cmd == "run":
    if os.environ.get("FAKE_DOCKER_SLEEP"):
        time.sleep(float(os.environ["FAKE_DOCKER_SLEEP"]))
    print("some library warning on stdout")
    print(json.dumps({
        "result": os.environ.get("FAKE_DOCKER_RESULT", "ROOT CAUSE: fixed\\nFIX: yes"),
        "usage": {"inputTokens": 1234, "outputTokens": 567},
        "toolCalls": 9,
        "sessionId": "sess-1",
    }))
    sys.exit(int(os.environ.get("FAKE_DOCKER_EXIT", "0")))
if cmd in ("stop", "rm", "kill"):
    sys.exit(0)
print("unexpected: " + " ".join(args), file=sys.stderr)
sys.exit(2)
'''

PROVIDER = ProviderConfig(
    name="test", protocol="openai", base_url="http://localhost", model="m", api_key=""
)


def make_fake_runtime(tmp_path: Path) -> tuple[str, Path, Path]:
    """造一个假的 docker 可执行文件，返回 (可执行路径, argv 记录文件, 状态文件)。"""
    script = tmp_path / "fake_docker.py"
    script.write_text(FAKE_DOCKER, encoding="utf-8")
    record = tmp_path / "argv.jsonl"
    state = tmp_path / "state.json"

    if sys.platform == "win32":
        wrapper = tmp_path / "fake-docker.cmd"
        wrapper.write_text(
            f'@echo off\r\n"{sys.executable}" "{script}" %*\r\n', encoding="utf-8"
        )
    else:
        wrapper = tmp_path / "fake-docker"
        wrapper.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n', encoding="utf-8")
        wrapper.chmod(0o755)
    return str(wrapper), record, state


def make_sandbox(tmp_path: Path, **config_overrides) -> tuple[DockerSandbox, Path]:
    runtime, record, state = make_fake_runtime(tmp_path)
    config = SandboxConfig(**config_overrides)
    src = tmp_path / "mewcode-src"
    src.mkdir(exist_ok=True)
    sandbox = DockerSandbox(
        config, mewcode_src=str(Path(__file__).resolve().parents[1]), runtime_bin=runtime,
        work_root=str(tmp_path / "sandbox"),
    )
    import os

    os.environ["FAKE_DOCKER_RECORD"] = str(record)
    os.environ["FAKE_DOCKER_STATE"] = str(state)
    return sandbox, record


def recorded(record: Path) -> list[list[str]]:
    if not record.exists():
        return []
    return [json.loads(line) for line in record.read_text(encoding="utf-8").splitlines() if line.strip()]


# =========================================================================
# A. 隔离与限额（安全属性逐项断言）
# =========================================================================

class TestIsolationFlags:
    def make_args(self, tmp_path: Path, **overrides) -> list[str]:
        sandbox, _ = make_sandbox(tmp_path, **overrides)
        run_dir = tmp_path / "run"
        run_dir.mkdir(exist_ok=True)
        (run_dir / "config.yaml").write_text("providers: []\n", encoding="utf-8")
        (run_dir / "prompt.txt").write_text("do it", encoding="utf-8")
        return sandbox.build_run_args(
            name="mewcode-job-1",
            image="img:tag",
            work_dir=str(tmp_path / "wt"),
            shell_command=sandbox.agent_shell_command(),
            config_path=str(run_dir / "config.yaml"),
            prompt_path=str(run_dir / "prompt.txt"),
            env={"OPENAI_API_KEY": "k"},
        )

    def test_only_worktree_source_and_config_are_mounted(self, tmp_path: Path):
        args = self.make_args(tmp_path)
        mounts = [args[i + 1] for i, a in enumerate(args) if a == "-v"]
        assert len(mounts) == 4, mounts
        assert any(m.endswith(":/workspace") for m in mounts)          # 只有 job 的 worktree
        assert any(m.endswith(":/opt/mewcode:ro") for m in mounts)     # 只读源码
        assert any(m.endswith(":/etc/mewcode/config.yaml:ro") for m in mounts)
        assert any(m.endswith(":/tmp/mewcode-prompt.txt:ro") for m in mounts)
        # 宿主 config.yaml（含 LLM API key）不得被挂载
        assert not any("mewcode\\config.yaml" in m or m.endswith(".mewcode/config.yaml:ro") for m in mounts)

    def test_least_privilege_flags(self, tmp_path: Path):
        args = self.make_args(tmp_path)
        assert "--cap-drop" in args and args[args.index("--cap-drop") + 1] == "ALL"
        assert "no-new-privileges" in " ".join(args)
        assert "--read-only" in args
        assert any(a.startswith("/tmp:rw,size=") for a in args)  # 只有 /tmp 可写 tmpfs

    def test_resource_limits(self, tmp_path: Path):
        args = self.make_args(tmp_path, cpus=1.5, memory="2g", pids_limit=256)
        assert args[args.index("--cpus") + 1] == "1.5"
        assert args[args.index("--memory") + 1] == "2g"
        assert args[args.index("--pids-limit") + 1] == "256"

    def test_non_root_user(self, tmp_path: Path):
        args = self.make_args(tmp_path, user="")
        user = args[args.index("--user") + 1]
        assert user and user != "0" and not user.startswith("0:")
        assert args[args.index("--user") + 1] == sandbox_user(SandboxConfig(user=""))

    def test_explicit_user_wins(self, tmp_path: Path):
        assert sandbox_user(SandboxConfig(user="4242:4242")) == "4242:4242"

    def test_network_policy(self, tmp_path: Path):
        assert self.make_args(tmp_path, network="none")[self.make_args(tmp_path, network="none").index("--network") + 1] == "none"
        assert self.make_args(tmp_path, network="bridge")[self.make_args(tmp_path, network="bridge").index("--network") + 1] == "bridge"

    def test_prompt_not_in_argv(self, tmp_path: Path):
        """提示词走文件挂载——不进 argv（长度与注入都不是问题）。"""
        args = self.make_args(tmp_path)
        assert "--prompt" not in " ".join(args)
        shell_cmd = args[-1]
        assert "mewcode-prompt.txt" in shell_cmd
        assert "do it" not in " ".join(args)

    def test_container_runs_headless_agent_json_mode(self, tmp_path: Path):
        args = self.make_args(tmp_path)
        assert args[-3:] == [
            "sh", "-c",
            'python -m mewcode -p "$(cat /tmp/mewcode-prompt.txt)" --output-format json '
            "--config /etc/mewcode/config.yaml --mode dontAsk",
        ]

    def test_rm_by_default_keep_flag(self, tmp_path: Path):
        assert "--rm" in self.make_args(tmp_path)
        assert "--rm" not in self.make_args(tmp_path, keep_containers=True)


# =========================================================================
# B. 探测与降级
# =========================================================================

class TestAvailability:
    @pytest.mark.asyncio
    async def test_missing_runtime_is_unavailable(self, tmp_path: Path):
        sandbox = DockerSandbox(
            SandboxConfig(), mewcode_src=str(tmp_path), runtime_bin=str(tmp_path / "nope-docker")
        )
        assert await sandbox.available() is False

    @pytest.mark.asyncio
    async def test_fake_runtime_available(self, tmp_path: Path):
        sandbox, record = make_sandbox(tmp_path)
        assert await sandbox.available() is True
        assert recorded(record)[0][:1] == ["info"]

    @pytest.mark.asyncio
    async def test_unavailable_raises_on_run(self, tmp_path: Path):
        sandbox = DockerSandbox(
            SandboxConfig(), mewcode_src=str(tmp_path), runtime_bin=str(tmp_path / "nope-docker")
        )
        with pytest.raises(SandboxUnavailable):
            await sandbox.run_agent("job-1", str(tmp_path), "p", PROVIDER, timeout=5)

    @pytest.mark.asyncio
    async def test_availability_is_cached(self, tmp_path: Path):
        sandbox, record = make_sandbox(tmp_path)
        await sandbox.available()
        await sandbox.available()
        assert len([c for c in recorded(record) if c[0] == "info"]) == 1


# =========================================================================
# C. 镜像（内容寻址 + 缓存）
# =========================================================================

class TestImage:
    @pytest.mark.asyncio
    async def test_image_builds_once_then_cached(self, tmp_path: Path):
        sandbox, record = make_sandbox(tmp_path)
        tag1 = await sandbox.ensure_image("demo", str(tmp_path))
        tag2 = await sandbox.ensure_image("demo", str(tmp_path))
        assert tag1 == tag2
        builds = [c for c in recorded(record) if c[0] == "build"]
        assert len(builds) == 1

    @pytest.mark.asyncio
    async def test_image_tag_changes_with_project_requirements(self, tmp_path: Path):
        sandbox, _ = make_sandbox(tmp_path)
        (tmp_path / "requirements.txt").write_text("flask\n", encoding="utf-8")
        tag_with_req = sandbox.image_tag("demo", sandbox.dockerfile(), "flask\n")
        tag_without = sandbox.image_tag("demo", sandbox.dockerfile(), "")
        assert tag_with_req != tag_without

    def test_image_tag_changes_with_mewcode_requirements(self, tmp_path: Path):
        """内核依赖进摘要：锁文件一变就必须重建镜像。

        反例（真机踩到）：只哈希 Dockerfile 时，宿主升级了 mcp 而容器继续
        复用旧镜像，容器内的内核版本悄悄落后。
        """
        sandbox, _ = make_sandbox(tmp_path)
        dockerfile = sandbox.dockerfile()
        base = sandbox.image_tag("demo", dockerfile, "")
        bumped = sandbox.image_tag("demo", dockerfile, "", "mcp==1.27.0\n")
        assert base != bumped

    @pytest.mark.asyncio
    async def test_mewcode_requirements_prefer_locked_export(self, tmp_path: Path):
        sandbox, _ = make_sandbox(tmp_path)
        calls: list[list[str]] = []

        async def fake_run(argv, timeout, cwd=None):
            calls.append(argv)
            return 0, "mcp==1.27.0\nhttpx==0.28.1\n"

        sandbox._run = fake_run  # type: ignore[method-assign]
        reqs = await sandbox.mewcode_requirements()
        assert reqs == "mcp==1.27.0\nhttpx==0.28.1\n"
        assert calls[0][:2] == ["uv", "export"]
        # 导出是只读操作：不能改锁文件
        assert "--frozen" in calls[0]

    @pytest.mark.asyncio
    async def test_mewcode_requirements_fall_back_when_uv_missing(self, tmp_path: Path):
        sandbox, _ = make_sandbox(tmp_path)

        async def fake_run(argv, timeout, cwd=None):
            if argv[0] == "uv":
                return 1, "(cannot spawn uv: [Errno 2])"
            return 0, "textual>=2.1.0\nmcp>=1.12.0\n"

        sandbox._run = fake_run  # type: ignore[method-assign]
        reqs = await sandbox.mewcode_requirements()
        assert "mcp>=1.12.0" in reqs

    def test_dockerfile_installs_mewcode_and_project_deps(self, tmp_path: Path):
        sandbox, _ = make_sandbox(tmp_path)
        dockerfile = sandbox.dockerfile()
        assert dockerfile.startswith(f"FROM {SandboxConfig().base_image}")
        assert "mewcode-requirements.txt" in dockerfile
        assert "project-requirements.txt" in dockerfile
        assert "PYTHONDONTWRITEBYTECODE=1" in dockerfile      # 容器里也要关掉 pyc（秒级 mtime 陷阱）

    @pytest.mark.asyncio
    async def test_build_context_has_no_repo_code(self, tmp_path: Path):
        """构建上下文只放依赖清单——业务代码不进镜像。"""
        sandbox, _ = make_sandbox(tmp_path)
        await sandbox.ensure_image("demo", str(tmp_path))
        contexts = list((tmp_path / "sandbox" / "build").glob("*"))
        assert contexts, "no build context created"
        files = sorted(p.name for p in contexts[0].iterdir())
        assert files == ["Dockerfile", "mewcode-requirements.txt", "project-requirements.txt"]


# =========================================================================
# D. 运行与结果解析
# =========================================================================

class TestRunAgent:
    @pytest.mark.asyncio
    async def test_happy_path_result_parsed(self, tmp_path: Path):
        sandbox, record = make_sandbox(tmp_path)
        result = await sandbox.run_agent(
            "job-42", str(tmp_path), "fix it", PROVIDER, repo_name="demo", timeout=60
        )
        assert result.ok
        assert "ROOT CAUSE" in result.result_text
        assert (result.input_tokens, result.output_tokens, result.tool_calls) == (1234, 567, 9)
        assert result.container == "mewcode-job-42"
        runs = [c for c in recorded(record) if c[0] == "run"]
        assert runs and runs[0][runs[0].index("--name") + 1] == "mewcode-job-42"

    @pytest.mark.asyncio
    async def test_container_config_has_no_secret(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-super-secret")
        sandbox, _ = make_sandbox(tmp_path)
        run_dir = tmp_path / "sandbox" / "runs" / "mewcode-job-7"
        run_dir.mkdir(parents=True, exist_ok=True)
        path = sandbox.write_container_config(run_dir / "config.yaml", PROVIDER)
        text = path.read_text(encoding="utf-8")
        assert "sk-super-secret" not in text
        assert 'api_key: ""' in text or "api_key: ''" in text

    @pytest.mark.asyncio
    async def test_llm_key_passed_via_env_only(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-super-secret")
        sandbox, record = make_sandbox(tmp_path)

        captured: dict = {}
        await sandbox.run_agent("job-9", str(tmp_path), "p", PROVIDER, repo_name="demo", timeout=60)
        runs = [c for c in recorded(record) if c[0] == "run"]
        flags = runs[0]
        captured["env"] = [flags[i + 1] for i, a in enumerate(flags) if a == "-e"]
        assert any(e.startswith("OPENAI_API_KEY=") for e in captured["env"])
        # 密钥不出现在挂载点或命令里
        assert not any("sk-super-secret" in m for m in flags if ":/" in m)

    @pytest.mark.asyncio
    async def test_env_passthrough_excludes_unlisted_keys(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-ok")
        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "aws-secret")
        sandbox, record = make_sandbox(tmp_path)
        await sandbox.run_agent("job-10", str(tmp_path), "p", PROVIDER, repo_name="demo", timeout=60)
        flags = [c for c in recorded(record) if c[0] == "run"][0]
        env = [flags[i + 1] for i, a in enumerate(flags) if a == "-e"]
        assert not any("AWS_SECRET" in e for e in env), "白名单外的宿主凭证不得进入容器"

    @pytest.mark.asyncio
    async def test_timeout_stops_container_and_reports(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("FAKE_DOCKER_SLEEP", "30")
        sandbox, record = make_sandbox(tmp_path)
        result = await sandbox.run_agent("job-11", str(tmp_path), "p", PROVIDER, repo_name="demo", timeout=1)
        assert result.timed_out is True
        assert result.ok is False
        stops = [c for c in recorded(record) if c[0] == "stop"]
        assert stops, "超时必须显式停容器（不能依赖进程退出兜底）"

    @pytest.mark.asyncio
    async def test_nonzero_exit_reported(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("FAKE_DOCKER_EXIT", "1")
        sandbox, _ = make_sandbox(tmp_path)
        result = await sandbox.run_agent("job-12", str(tmp_path), "p", PROVIDER, repo_name="demo", timeout=30)
        assert result.exit_code == 1 and not result.ok

    @pytest.mark.asyncio
    async def test_network_override_per_run(self, tmp_path: Path):
        sandbox, record = make_sandbox(tmp_path, network="bridge")
        await sandbox.run_agent(
            "job-13", str(tmp_path), "p", PROVIDER, repo_name="demo", timeout=30, network="none"
        )
        flags = [c for c in recorded(record) if c[0] == "run"][0]
        assert flags[flags.index("--network") + 1] == "none"


class TestOutputParsing:
    def parse(self, output: str, code: int = 0):
        sandbox = DockerSandbox(SandboxConfig(), mewcode_src=".")
        return sandbox._parse_output(code, output, False, "c")

    def test_noise_before_json_is_ignored(self):
        out = 'warning: something\n{"result": "done", "usage": {"inputTokens": 5, "outputTokens": 6}, "toolCalls": 2}'
        result = self.parse(out)
        assert result.result_text == "done" and result.input_tokens == 5 and result.tool_calls == 2

    def test_result_containing_braces_is_fine(self):
        payload = {"result": "FIX: use dict {a: 1}", "usage": {"inputTokens": 1, "outputTokens": 1}, "toolCalls": 0}
        result = self.parse(json.dumps(payload))
        assert "dict {a: 1}" in result.result_text

    def test_garbage_output_yields_empty_result(self):
        result = self.parse("no json here at all")
        assert result.result_text == "" and result.input_tokens == 0

    def test_json_without_result_key_ignored(self):
        result = self.parse('{"unrelated": true}')
        assert result.result_text == ""


# =========================================================================
# E. 接入执行链：沙箱模式、降级、超时
# =========================================================================

class TestRunnerIntegration:
    """HeadlessAgentRunner 的沙箱分支与自动降级（M2 验收标准 4）。"""

    def make_runner(self, tmp_path: Path, sandbox):
        from mewcode.config import ServiceConfig
        from mewcode.service.execution import HeadlessAgentRunner

        return HeadlessAgentRunner(ServiceConfig(), PROVIDER, sandbox=sandbox)

    @pytest.mark.asyncio
    async def test_sandbox_used_when_available(self, tmp_path: Path):
        from mewcode.service.execution import AgentRunOutcome  # noqa: F401
        from mewcode.service.jobs import Job

        sandbox, record = make_sandbox(tmp_path)
        job = Job(id="job-77", fingerprint="f", repo="demo", severity="w", status="fixing", payload={})
        runner = self.make_runner(tmp_path, sandbox)
        events: list[dict] = []
        outcome = await runner.run(job, str(tmp_path), "fix it", events.append)

        assert "ROOT CAUSE" in outcome.final_text
        assert (outcome.tool_calls, outcome.input_tokens) == (9, 1234)
        assert any(e.get("type") == "sandbox" for e in events)
        assert any(e.get("type") == "usage" and e["usage"]["inputTokens"] == 1234 for e in events)
        assert [c for c in recorded(record) if c[0] == "run"], "must run inside the container"

    @pytest.mark.asyncio
    async def test_fallback_to_direct_when_unavailable(self, tmp_path: Path, monkeypatch):
        """无容器运行时 -> 直跑 M1 模式（并留下 warning），作业不失败。"""
        from mewcode.service import execution as ex
        from mewcode.service.jobs import Job

        sandbox = DockerSandbox(
            SandboxConfig(), mewcode_src=str(tmp_path), runtime_bin=str(tmp_path / "nope")
        )
        runner = self.make_runner(tmp_path, sandbox)

        called: dict = {}

        async def fake_direct(job, work_dir, prompt, on_event):
            called["direct"] = True
            return ex.AgentRunOutcome(final_text="direct mode", tool_calls=1, input_tokens=2, output_tokens=3)

        monkeypatch.setattr(runner, "_run_direct", fake_direct)
        job = Job(id="job-78", fingerprint="f", repo="demo", severity="w", status="fixing", payload={})
        outcome = await runner.run(job, str(tmp_path), "p", lambda e: None)
        assert called.get("direct") is True
        assert outcome.final_text == "direct mode"

    @pytest.mark.asyncio
    async def test_sandbox_disabled_in_config_skips_probe(self, tmp_path: Path, monkeypatch):
        from mewcode.config import ServiceConfig
        from mewcode.service.execution import HeadlessAgentRunner
        from mewcode.service.jobs import Job

        sandbox, record = make_sandbox(tmp_path)
        config = ServiceConfig()
        config.sandbox.enabled = False
        runner = HeadlessAgentRunner(config, PROVIDER, sandbox=sandbox)
        calls = {"n": 0}

        async def fake_direct(job, work_dir, prompt, on_event):
            calls["n"] += 1
            return runner.__class__.__mro__[0].__dict__ and __import__(
                "mewcode.service.execution", fromlist=["AgentRunOutcome"]
            ).AgentRunOutcome(final_text="x")

        monkeypatch.setattr(runner, "_run_direct", fake_direct)
        job = Job(id="job-79", fingerprint="f", repo="demo", severity="w", status="fixing", payload={})
        await runner.run(job, str(tmp_path), "p", lambda e: None)
        assert calls["n"] == 1
        assert not [c for c in recorded(record) if c[0] == "run"]

    @pytest.mark.asyncio
    async def test_sandbox_timeout_raises(self, tmp_path: Path, monkeypatch):
        """容器止损超时 -> TimeoutError（执行链据此 escalate，而不是静默成功）。"""
        from mewcode.service.jobs import Job
        from mewcode.service.sandbox import SandboxRunResult

        sandbox, _ = make_sandbox(tmp_path)
        runner = self.make_runner(tmp_path, sandbox)

        async def timed_out_run(*args, **kwargs):
            return SandboxRunResult(exit_code=124, timed_out=True, container="c")

        monkeypatch.setattr(sandbox, "run_agent", timed_out_run)
        job = Job(id="job-80", fingerprint="f", repo="demo", severity="w", status="fixing", payload={})
        with pytest.raises(TimeoutError, match="timed out"):
            await runner.run(job, str(tmp_path), "p", lambda e: None)

    @pytest.mark.asyncio
    async def test_sandbox_failure_without_result_raises(self, tmp_path: Path, monkeypatch):
        """容器非零退出且没有 JSON 结果 -> 带容器日志的 RuntimeError。"""
        from mewcode.service.jobs import Job
        from mewcode.service.sandbox import SandboxRunResult

        sandbox, _ = make_sandbox(tmp_path)
        runner = self.make_runner(tmp_path, sandbox)

        async def failed_run(*args, **kwargs):
            return SandboxRunResult(exit_code=2, stdout="Traceback: boom", container="c")

        monkeypatch.setattr(sandbox, "run_agent", failed_run)
        job = Job(id="job-81", fingerprint="f", repo="demo", severity="w", status="fixing", payload={})
        with pytest.raises(RuntimeError, match="exited with 2"):
            await runner.run(job, str(tmp_path), "p", lambda e: None)


class TestSandboxTestRunner:
    @pytest.mark.asyncio
    async def test_runs_command_in_container(self, tmp_path: Path):
        from mewcode.service.execution import SandboxTestRunner

        sandbox, record = make_sandbox(tmp_path)
        runner = SandboxTestRunner(sandbox, repo_name="demo")
        outcome = await runner.run(str(tmp_path), "python -c 'print(1)'", 30)
        assert outcome.exit_code == 0
        runs = [c for c in recorded(record) if c[0] == "run"]
        assert runs and runs[0][-1] == "python -c 'print(1)'"
        # 验证类命令不挂载 mewcode 源码（只挂 worktree）
        assert not any(m.endswith(":/opt/mewcode:ro") for m in runs[0] if ":/" in m)

    @pytest.mark.asyncio
    async def test_falls_back_to_host_when_unavailable(self, tmp_path: Path):
        from mewcode.service.execution import SandboxTestRunner

        sandbox = DockerSandbox(
            SandboxConfig(), mewcode_src=str(tmp_path), runtime_bin=str(tmp_path / "nope")
        )
        runner = SandboxTestRunner(sandbox, repo_name="demo")
        outcome = await runner.run(str(tmp_path), f'"{sys.executable}" -c "print(42)"', 30)
        assert outcome.exit_code == 0 and "42" in outcome.output   # 宿主直跑兜底


class TestNeverRootByDefault:
    """宿主以 root 跑服务时（容器化部署常见），沙箱也不能跟着用 0。"""

    def test_root_host_falls_back_to_unprivileged(self, monkeypatch):
        import mewcode.service.sandbox as sbx

        monkeypatch.setattr(sbx.sys, "platform", "linux")
        monkeypatch.setattr(sbx.os, "getuid", lambda: 0, raising=False)
        monkeypatch.setattr(sbx.os, "getgid", lambda: 0, raising=False)
        assert sbx.sandbox_user(SandboxConfig()) == "1000:1000"

    def test_non_root_host_matches_host_uid(self, monkeypatch):
        import mewcode.service.sandbox as sbx

        monkeypatch.setattr(sbx.sys, "platform", "linux")
        monkeypatch.setattr(sbx.os, "getuid", lambda: 4242, raising=False)
        monkeypatch.setattr(sbx.os, "getgid", lambda: 4242, raising=False)
        assert sbx.sandbox_user(SandboxConfig()) == "4242:4242"

    def test_windows_defaults_unprivileged(self):
        assert sandbox_user(SandboxConfig()) not in ("0", "0:0")
