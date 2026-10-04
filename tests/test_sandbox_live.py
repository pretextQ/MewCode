"""M2 W1 真机沙箱验证（需要可用的容器运行时）。

默认跳过（会让 CI 变慢且要拉基础镜像）：用 ``MEWCODE_DOCKER_TESTS=1`` 打开。
验证的是**隔离本身**，不是"能跑"：非 root、根文件系统只读、宿主文件不可见、
无网策略生效、限额生效、容器退出后无残留。
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from mewcode.config import SandboxConfig
from mewcode.service.sandbox import DockerSandbox

pytestmark = pytest.mark.skipif(
    os.environ.get("MEWCODE_DOCKER_TESTS") != "1",
    reason="set MEWCODE_DOCKER_TESTS=1 to run live container isolation checks",
)


def docker_available() -> bool:
    try:
        return subprocess.run(
            ["docker", "info"], capture_output=True, timeout=30
        ).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


@pytest.fixture(scope="module")
def sandbox(tmp_path_factory) -> DockerSandbox:
    if not docker_available():
        pytest.skip("docker daemon not available")
    src = Path(__file__).resolve().parents[1]
    return DockerSandbox(
        SandboxConfig(base_image="python:3.12-slim", cpus=1.0, memory="1g", pids_limit=128),
        mewcode_src=str(src),
        work_root=str(tmp_path_factory.mktemp("sandbox-live")),
    )


@pytest.fixture
def worktree(tmp_path: Path) -> Path:
    wt = tmp_path / "wt"
    wt.mkdir()
    (wt / "hello.txt").write_text("inside worktree", encoding="utf-8")
    return wt


@pytest.mark.asyncio
async def test_runs_as_non_root(sandbox: DockerSandbox, worktree: Path):
    result = await sandbox.run_command("live-uid", str(worktree), "id -u", timeout=600)
    assert result.exit_code == 0
    uid = result.stdout.strip().splitlines()[-1]
    assert uid.strip() not in ("0", "root"), f"container ran as root: {result.stdout!r}"


@pytest.mark.asyncio
async def test_worktree_is_writable_rootfs_is_not(sandbox: DockerSandbox, worktree: Path):
    script = "echo written > /workspace/new.txt && cat /workspace/new.txt; echo '---'; touch /nope 2>&1 | tail -1"
    result = await sandbox.run_command("live-write", str(worktree), script, timeout=600)
    out = result.stdout
    assert "written" in out, out
    assert (worktree / "new.txt").read_text(encoding="utf-8").strip() == "written"
    assert "Read-only file system" in out or "read-only file system" in out, out


@pytest.mark.asyncio
async def test_host_files_outside_mount_are_invisible(sandbox: DockerSandbox, worktree: Path):
    """宿主上的敏感路径在容器里不存在——这是"只挂 worktree"的实际效果。"""
    host_secret = Path.home() / ".mewcode" / "config.yaml"
    script = f"ls -la '{host_secret.as_posix()}' 2>&1 | tail -1; echo '---'; ls /workspace"
    result = await sandbox.run_command("live-invisible", str(worktree), script, timeout=600)
    assert "No such file" in result.stdout, result.stdout
    assert "hello.txt" in result.stdout


@pytest.mark.asyncio
async def test_network_none_blocks_egress(sandbox: DockerSandbox, worktree: Path):
    script = "python - <<'PY'\nimport socket\nsocket.setdefaulttimeout(5)\ntry:\n    socket.create_connection(('1.1.1.1', 443))\n    print('CONNECTED')\nexcept OSError as e:\n    print('BLOCKED', type(e).__name__)\nPY"
    result = await sandbox.run_command(
        "live-nonet", str(worktree), script, timeout=600, network="none"
    )
    assert "BLOCKED" in result.stdout and "CONNECTED" not in result.stdout, result.stdout


@pytest.mark.asyncio
async def test_bridge_network_allows_egress(sandbox: DockerSandbox, worktree: Path):
    """bridge 模式必须能出网——agent 要靠它访问 LLM API。"""
    script = "python - <<'PY'\nimport socket\nsocket.setdefaulttimeout(10)\ntry:\n    socket.create_connection(('1.1.1.1', 443))\n    print('CONNECTED')\nexcept OSError as e:\n    print('BLOCKED', type(e).__name__)\nPY"
    result = await sandbox.run_command(
        "live-net", str(worktree), script, timeout=600, network="bridge"
    )
    assert "CONNECTED" in result.stdout, result.stdout


@pytest.mark.asyncio
async def test_pids_limit_is_enforced(sandbox: DockerSandbox, worktree: Path):
    script = (
        "python - <<'PY'\n"
        "import os, sys\n"
        "kids = []\n"
        "try:\n"
        "    for _ in range(300):\n"
        "        kids.append(os.fork())\n"
        "    print('NO_LIMIT')\n"
        "except OSError as e:\n"
        "    print('BLOCKED', e.errno)\n"
        "finally:\n"
        "    for pid in kids:\n"
        "        os.waitpid(pid, 0)\n"
        "PY"
    )
    result = await sandbox.run_command("live-pids", str(worktree), script, timeout=600)
    assert "BLOCKED" in result.stdout, result.stdout


@pytest.mark.asyncio
async def test_agent_json_output_roundtrip(sandbox: DockerSandbox, worktree: Path):
    """容器内跑 mewcode 的 --output-format json 通路（不调 LLM，只验证 _parse_output 契约）。"""
    result = await sandbox.run_command(
        "live-json",
        str(worktree),
        'python -c "import json;print(json.dumps({\'result\':\'ok\',\'usage\':{\'inputTokens\':1,\'outputTokens\':2},\'toolCalls\':0}))"',
        timeout=600,
    )
    parsed = sandbox._parse_output(result.exit_code, result.stdout, False, "c")
    assert parsed.result_text == "ok" and parsed.input_tokens == 1


@pytest.mark.asyncio
async def test_no_container_left_behind(sandbox: DockerSandbox, worktree: Path):
    names = ["mewcode-live-cleanup-a", "mewcode-live-cleanup-b"]
    for name in names:
        result = await sandbox.run_command(name, str(worktree), "echo done", timeout=600)
        assert result.exit_code == 0
    ps = subprocess.run(
        ["docker", "ps", "-a", "--filter", "name=mewcode-live-cleanup", "--format", "{{.Names}}"],
        capture_output=True, text=True, timeout=60,
    )
    assert not ps.stdout.strip(), f"containers leaked: {ps.stdout}"


# ---------------------------------------------------------------------------
# M2 W3：内部工具链（只读 MCP server）在容器内跑起来
# ---------------------------------------------------------------------------

MCP_PROBE_SCRIPT = """\
python /opt/mewcode/tests/helpers/fake_backends.py --loki-port 8899 --github-port 8900 >/tmp/backends.log 2>&1 &
ok=1
for i in 1 2 3 4 5; do
  if python /opt/mewcode/tests/helpers/mcp_probe.py --config-file {config} --tool {tool} --tool-args-file {args} >/tmp/probe.out 2>/tmp/probe.err; then
    ok=0
    break
  fi
  sleep 2
done
echo "PROBE_OK=$ok"
cat /tmp/probe.out 2>/dev/null
echo "--- probe.err"
cat /tmp/probe.err 2>/dev/null
echo "--- backends.log"
cat /tmp/backends.log 2>/dev/null
"""


async def _probe_mcp_in_container(
    sandbox: DockerSandbox, worktree: Path, *, name: str, server: dict, tool: str, tool_args: dict
):
    """在容器里起假后端 + 探针（与 agent 同一条 MCP 接线路径），返回容器输出。"""
    import json

    (worktree / "mcp-server.json").write_text(json.dumps(server), encoding="utf-8")
    (worktree / "mcp-args.json").write_text(json.dumps(tool_args), encoding="utf-8")
    script = MCP_PROBE_SCRIPT.format(
        config="/workspace/mcp-server.json", args="/workspace/mcp-args.json", tool=tool
    )
    return await sandbox.run_command(name, str(worktree), script, timeout=900, mount_src=True)


@pytest.mark.asyncio
async def test_logs_mcp_server_works_inside_the_container(sandbox: DockerSandbox, worktree: Path):
    """容器内：假 Loki → MCP stdio server → 工具调用，数据要真的回得来。

    这条链覆盖了容器模式的全部关键点：源码只读挂载（PYTHONPATH）、依赖已在
    镜像里、server 进程在容器内由 agent 自己拉起、结果经 MCP 协议回到调用方。
    """
    result = await _probe_mcp_in_container(
        sandbox,
        worktree,
        name="live-mcp-logs",
        server={
            "name": "logs",
            "command": "python",
            "args": ["-m", "mewcode.mcp.servers.logs"],
            "env": {"MEWCODE_LOKI_URL": "http://127.0.0.1:8899"},
        },
        tool="query_logs",
        tool_args={"query": '{app="checkout"}'},
    )
    out = result.stdout
    assert "PROBE_OK=0" in out, out
    assert "TIMEOUT_SECONDS must be > 0" in out, out
    # 注册表里就是这两个只读工具（服务模式只挂只读工具，容器里同样成立）
    assert "mcp_logs_query_logs" in out and "mcp_logs_list_labels" in out, out


@pytest.mark.asyncio
async def test_ci_mcp_server_works_inside_the_container(sandbox: DockerSandbox, worktree: Path):
    result = await _probe_mcp_in_container(
        sandbox,
        worktree,
        name="live-mcp-ci",
        server={
            "name": "ci",
            "command": "python",
            "args": ["-m", "mewcode.mcp.servers.ci"],
            "env": {"MEWCODE_GITHUB_API": "http://127.0.0.1:8900"},
        },
        tool="get_check_runs",
        tool_args={"repo": "demo/repo", "ref": "main"},
    )
    out = result.stdout
    assert "PROBE_OK=0" in out, out
    assert "pytest (ubuntu-latest)" in out, out
