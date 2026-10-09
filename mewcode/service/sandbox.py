"""Docker 沙箱执行器（M2 W1）。

设计取舍：
- **agent 在容器内跑**：容器里执行 ``python -m mewcode -p ... --output-format json``，
  宿主只给容器挂载该 job 的 worktree 与只读的 mewcode 源码。这样 OS 级隔离
  覆盖"文件 + 进程 + 网络"三件事，而不是只把 Bash 塞进容器。
- **密钥不进镜像也不进 worktree**：容器内的最小配置里 ``api_key`` 留空，
  LLM key 只在 ``docker run -e`` 的容器环境变量里（宿主 config.yaml 不被挂载）。
- **默认最小权限**：非 root 用户、``--cap-drop=ALL``、``no-new-privileges``、
  只读根文件系统 + /tmp tmpfs、CPU/内存/PID 限额、硬超时后强制杀容器。
- **网络**：``bridge``（agent 必须能访问 LLM API）或 ``none``（完全隔离，用于
  纯验证类执行）。细粒度 egress 白名单（只放行 LLM API 与 git 远端）需要
  宿主侧 iptables/DOCKER-USER 规则或代理，见 docs/fixes/backlog.md。
- **内部工具链也在容器里**（M2 W3）：MCP server 配置随最小配置进容器，
  stdio server 由容器内的 agent 进程自己拉起——宿主上不需要装那些工具，
  容器退出即全部消失。server 需要的凭据只经环境变量白名单进容器。
- **默认拒绝降级**：运行时不可用时 ``available()`` 为假，调用方默认拒绝执行；
  只有显式开启 ``allow_host_fallback`` 才能在无 OS 隔离的宿主执行。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import shutil
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from mewcode.config import SandboxConfig
from mewcode.processes import create_exec_process, kill_process_tree, release_process

log = logging.getLogger(__name__)

PROBE_TIMEOUT = 30
IMAGE_BUILD_TIMEOUT = 900
REQUIREMENTS_TIMEOUT = 120
CONTAINER_STOP_GRACE = 10
#: 容器内挂载点
SRC_MOUNT = "/opt/mewcode"
CONFIG_MOUNT = "/etc/mewcode/config.yaml"
PROMPT_MOUNT = "/tmp/mewcode-prompt.txt"


class SandboxError(Exception):
    """沙箱层的基类异常。"""


class SandboxUnavailable(SandboxError):
    """容器运行时不可用（未安装 / daemon 未运行 / 无权限）。"""


async def run_host_command(
    argv: list[str], timeout: float, cwd: str | None = None
) -> tuple[int, str]:
    """跑一个宿主侧命令，带硬超时与显式收尾（沙箱与 compose 编排共用）。

    返回 ``(exit_code, combined_output)``；超时返回 124 并杀掉进程树。
    """
    try:
        proc = await create_exec_process(
            *argv,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            stdin=asyncio.subprocess.DEVNULL,
        )
    except (OSError, ValueError) as e:
        return 1, f"(cannot spawn {argv[0]}: {e})"
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return proc.returncode or 0, (stdout or b"").decode("utf-8", errors="replace")
    except TimeoutError:
        await kill_process_tree(proc)
        return 124, f"(timed out after {timeout:.0f}s)"
    except asyncio.CancelledError:
        await kill_process_tree(proc)
        raise
    finally:
        release_process(proc)


@dataclass
class SandboxRunResult:
    exit_code: int
    result_text: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    tool_calls: int = 0
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    container: str = ""
    extra: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out


def sandbox_user(config: SandboxConfig) -> str:
    """非 root 运行身份：POSIX 上跟宿主 uid 对齐（挂载目录才不会写不进去）。

    宿主 uid 为 0（容器化部署、root 起服务）时**不能**跟着用 0——那等于把
    提权面重新打开；此时回落到镜像内置的 1000:1000。
    """
    if config.user:
        return config.user
    if sys.platform != "win32" and hasattr(os, "getuid"):
        uid = os.getuid()
        if uid != 0:
            return f"{uid}:{os.getgid()}"
    return "1000:1000"


def _mcp_config_payload(cfg) -> dict:
    """把 MCP server 配置转成容器配置里的 YAML 结构（字段与 validator 对齐）。"""
    payload: dict = {"name": cfg.name, "transport": cfg.transport}
    if cfg.command:
        payload["command"] = cfg.command
        payload["args"] = list(cfg.args)
    if cfg.url:
        payload["url"] = cfg.url
    if cfg.headers:
        payload["headers"] = dict(cfg.headers)
    if cfg.env:
        payload["env"] = dict(cfg.env)
    if cfg.description:
        payload["description"] = cfg.description
    return payload


def mcp_env_passthrough(mcp_servers: list | None) -> set[str]:
    """MCP 配置引用的宿主环境变量名（``${VAR}`` 形式，值不进配置文件）。"""
    from mewcode.config import find_env_placeholders

    names: set[str] = set()
    for cfg in mcp_servers or []:
        for value in list(getattr(cfg, "env", {}).values()) + list(getattr(cfg, "headers", {}).values()):
            if isinstance(value, str):
                names |= find_env_placeholders(value)
    return names


class DockerSandbox:
    """docker/podman 均可（命令结构一致，仅二进制名不同）。"""

    def __init__(
        self,
        config: SandboxConfig,
        *,
        mewcode_src: str,
        runtime_bin: str | None = None,
        work_root: str | None = None,
    ) -> None:
        self.config = config
        self.mewcode_src = str(Path(mewcode_src).resolve())
        self.runtime = runtime_bin or config.runtime or "docker"
        self.work_root = Path(work_root) if work_root else Path(tempfile.gettempdir()) / "mewcode-sandbox"
        self._available: bool | None = None
        self._requirements_cache: str = ""

    # -- 运行时探测 -------------------------------------------------------

    async def available(self) -> bool:
        """容器运行时是否可用（结果缓存；探测失败即视为不可用，不抛异常）。"""
        if self._available is not None:
            return self._available
        if shutil.which(self.runtime) is None:
            log.warning("sandbox: runtime '%s' not found on PATH", self.runtime)
            self._available = False
            return False
        code, out = await self._run([self.runtime, "info", "--format", "{{.ServerVersion}}"], timeout=PROBE_TIMEOUT)
        if code != 0:
            log.warning("sandbox: '%s info' failed, sandbox disabled: %s", self.runtime, out.strip()[:200])
            self._available = False
        else:
            self._available = True
        return self._available

    async def _run(self, argv: list[str], timeout: float, cwd: str | None = None) -> tuple[int, str]:
        """跑一个宿主侧命令（探测/构建/容器生命周期），带硬超时与显式收尾。"""
        return await run_host_command(argv, timeout, cwd)

    @staticmethod
    async def _kill_process_tree(proc: asyncio.subprocess.Process) -> None:
        await kill_process_tree(proc)

    # -- 镜像 -------------------------------------------------------------

    def dockerfile(self) -> str:
        """生成项目镜像的 Dockerfile（只装依赖，不 COPY 业务代码）。"""
        return "\n".join([
            f"FROM {self.config.base_image}",
            "RUN pip install --no-cache-dir uv",
            "ENV PYTHONDONTWRITEBYTECODE=1 PIP_DISABLE_PIP_VERSION_CHECK=1",
            "COPY mewcode-requirements.txt /tmp/mewcode-requirements.txt",
            "RUN uv pip install --system --no-cache -r /tmp/mewcode-requirements.txt",
            "COPY project-requirements.txt /tmp/project-requirements.txt",
            # 目标仓库没有依赖清单时生成的是空文件：跳过安装而不是失败
            'RUN if [ -s /tmp/project-requirements.txt ]; then '
            "uv pip install --system --no-cache -r /tmp/project-requirements.txt; "
            'else echo "no project requirements"; fi',
            "RUN useradd -m -u 1000 agent || true",
            f"WORKDIR {self.config.workdir}",
            "",
        ])

    async def mewcode_requirements(self) -> str:
        """MewCode 的运行依赖——**按 uv.lock 导出锁定版本**。

        容器里的内核必须与宿主同版本。此前直接读 pyproject 的宽松声明
        （``mcp>=1.12.0``），容器会解析出 mcp 2.x 而宿主是锁定的 1.27——
        容器内的 MCP server 一 import 就崩（真机实测踩到）。锁文件在就
        一律用它；导不出来再退化为宽松声明并记 warning。
        """
        if self._requirements_cache:
            return self._requirements_cache
        locked = await self._locked_requirements()
        if locked:
            self._requirements_cache = locked
            return locked

        script = (
            "import tomllib, pathlib;"
            "d=tomllib.loads(pathlib.Path('pyproject.toml').read_text(encoding='utf-8'));"
            "print('\\n'.join(d['project']['dependencies']))"
        )
        code, out = await self._run([sys.executable, "-c", script], timeout=60, cwd=self.mewcode_src)
        if code != 0 or not out.strip():
            raise SandboxError(f"cannot derive mewcode requirements: {out.strip()[:300]}")
        self._requirements_cache = out
        return out

    async def _locked_requirements(self) -> str:
        """``uv export --frozen``：不改锁文件、不装项目本身，只导出依赖钉版。"""
        code, out = await self._run(
            [
                "uv", "export", "--frozen", "--no-dev", "--no-emit-project",
                "--no-annotate", "--no-header",
            ],
            timeout=REQUIREMENTS_TIMEOUT,
            cwd=self.mewcode_src,
        )
        if code != 0 or not out.strip():
            log.warning(
                "sandbox: `uv export` unavailable, falling back to unpinned pyproject deps: %s",
                out.strip()[-300:],
            )
            return ""
        return out

    def image_tag(
        self,
        repo_name: str,
        dockerfile: str,
        project_requirements: str,
        mewcode_requirements: str = "",
    ) -> str:
        """内容寻址 tag：**依赖内容也算进摘要**。

        只哈希 Dockerfile 的话，锁文件更新后 tag 不变、旧镜像被继续复用——
        容器里的内核版本就悄悄落后于宿主（真机踩到过的 mcp 2.x 事故正是
        这一类）。三份内容一起进摘要，任何一份变了就重建。
        """
        digest = hashlib.sha256(
            (
                dockerfile
                + "\n--- mewcode deps ---\n"
                + mewcode_requirements
                + "\n--- project deps ---\n"
                + project_requirements
            ).encode("utf-8")
        ).hexdigest()[:12]
        safe_repo = "".join(c if c.isalnum() or c in "-_." else "-" for c in repo_name) or "repo"
        return f"{self.config.image_prefix}-{safe_repo}:{digest}"

    async def ensure_image(self, repo_name: str, repo_path: str | None = None) -> str:
        """确保项目镜像存在（内容寻址 tag：依赖不变就复用缓存）。"""
        dockerfile = self.dockerfile()
        mewcode_requirements = await self.mewcode_requirements()
        project_requirements = ""
        if repo_path:
            candidate = Path(repo_path) / "requirements.txt"
            if candidate.is_file():
                project_requirements = candidate.read_text(encoding="utf-8", errors="replace")
        tag = self.image_tag(repo_name, dockerfile, project_requirements, mewcode_requirements)

        code, _ = await self._run([self.runtime, "image", "inspect", tag], timeout=PROBE_TIMEOUT)
        if code == 0:
            return tag

        context = self.work_root / "build" / tag.replace(":", "_")
        context.mkdir(parents=True, exist_ok=True)
        (context / "Dockerfile").write_text(dockerfile, encoding="utf-8")
        (context / "mewcode-requirements.txt").write_text(mewcode_requirements, encoding="utf-8")
        (context / "project-requirements.txt").write_text(project_requirements, encoding="utf-8")

        log.info("sandbox: building image %s (this can take a while)", tag)
        code, out = await self._run(
            [self.runtime, "build", "-t", tag, "."], timeout=IMAGE_BUILD_TIMEOUT, cwd=str(context)
        )
        if code != 0:
            raise SandboxError(f"image build failed for {tag}: {out.strip()[-800:]}")
        return tag

    # -- 运行 -------------------------------------------------------------

    def container_name(self, job_id: str) -> str:
        safe = "".join(c if c.isalnum() or c in "-_." else "-" for c in job_id)
        return f"mewcode-{safe}"[:63]

    def build_run_args(
        self,
        *,
        name: str,
        image: str,
        work_dir: str,
        shell_command: str,
        config_path: str = "",
        prompt_path: str = "",
        env: dict[str, str] | None = None,
        network: str | None = None,
        mount_src: bool = False,
        extra_args: list[str] | None = None,
    ) -> list[str]:
        """组装 `docker run` 参数（纯函数，便于单测覆盖隔离与限额项）。

        ``shell_command`` 是容器内要跑的命令；给了 config/prompt 时同时挂载
        mewcode 源码与最小配置（agent 模式）。``mount_src`` 让验证类命令也能
        拿到只读源码（跑 mewcode 自身的内部工具时需要，例如 MCP server 探活）。
        ``extra_args`` 原样附在镜像名之前（如 ``--add-host``），调用方负责
        清楚自己加了什么——隔离项本身不从这里改。
        """
        env = env or {}
        agent_mode = bool(config_path and prompt_path)
        with_src = agent_mode or mount_src
        args = [self.runtime, "run", "--name", name]
        if not self.config.keep_containers:
            args.append("--rm")
        args += [
            "--workdir", self.config.workdir,
            "--user", sandbox_user(self.config),
            "--network", network or self.config.network,
            # 最小权限：能力全丢、禁止提权、根文件系统只读（只有 worktree 与 /tmp 可写）
            "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges",
            "--read-only",
            "--tmpfs", f"/tmp:rw,size={self.config.tmpfs_size}",
            # 资源限额：防 agent 死循环拖垮宿主机
            "--cpus", str(self.config.cpus),
            "--memory", self.config.memory,
            "--pids-limit", str(self.config.pids_limit),
            "-v", f"{Path(work_dir).resolve()}:{self.config.workdir}",
            "-e", "HOME=/tmp",
            "-e", "PYTHONDONTWRITEBYTECODE=1",
            # 调试日志写到容器 /tmp：cwd 是 worktree，写 .mewcode/ 会成为
            # "修复产物"的一部分（真机踩到：PR body 改动统计多出一个日志文件）
            "-e", "MEWCODE_LOG_FILE=/tmp/mewcode-debug.log",
        ]
        if agent_mode:
            args += [
                "-v", f"{Path(config_path).resolve()}:{CONFIG_MOUNT}:ro",
                "-v", f"{Path(prompt_path).resolve()}:{PROMPT_MOUNT}:ro",
            ]
        if with_src:
            args += [
                "-v", f"{self.mewcode_src}:{SRC_MOUNT}:ro",
                "-e", f"PYTHONPATH={SRC_MOUNT}",
            ]
        for key, value in sorted(env.items()):
            args += ["-e", f"{key}={value}"]
        args += list(extra_args or [])
        args += [image, "sh", "-c", shell_command]
        return args

    def agent_shell_command(self) -> str:
        """容器内的 agent 调用（提示词走文件，不进 argv）。"""
        return (
            f'python -m mewcode -p "$(cat {PROMPT_MOUNT})" --output-format json '
            f"--config {CONFIG_MOUNT} --mode dontAsk"
        )

    def write_container_config(
        self, path: Path, provider_config, mcp_servers: list | None = None
    ) -> Path:
        """写容器用的最小配置：不含任何密钥（key 只经环境变量注入）。

        MCP server 配置随配置进容器（M2 W3）：容器内的 agent 由这条路径
        拿到内部工具链。env 里只写 ``${VAR}`` 占位符，真实值经
        :meth:`container_env` 白名单透传——配置文件在容器里可读，密钥
        不能出现在里面。
        """
        import yaml

        payload: dict = {
            "providers": [
                {
                    "name": provider_config.name,
                    "protocol": provider_config.protocol,
                    "base_url": provider_config.base_url,
                    "model": provider_config.model,
                    "api_key": "",
                    "thinking": getattr(provider_config, "thinking", False),
                }
            ],
            "permission_mode": "dontAsk",
        }
        if mcp_servers:
            payload["mcp_servers"] = [_mcp_config_payload(cfg) for cfg in mcp_servers]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(yaml.safe_dump(payload, allow_unicode=True, sort_keys=False), encoding="utf-8")
        return path

    def container_env(self, provider_config, mcp_servers: list | None = None) -> dict[str, str]:
        """把白名单里的宿主环境变量透传给容器（LLM key 只走这条路）。

        ``mcp_servers`` 里 ``${VAR}`` 引用的变量名自动进白名单：运营方在
        配置里显式引用了它，就等于声明"这个名字要进容器"。
        """
        from mewcode.config import _ENV_KEY_MAP  # noqa: PLC0415 - 与 config 的映射保持一致

        env: dict[str, str] = {}
        needed = (
            {_ENV_KEY_MAP.get(provider_config.protocol, "")}
            | set(self.config.env_passthrough)
            | mcp_env_passthrough(mcp_servers)
        )
        for key in sorted(k for k in needed if k):
            value = os.environ.get(key, "")
            if value:
                env[key] = value
        return env

    async def run_agent(
        self,
        job_id: str,
        work_dir: str,
        prompt: str,
        provider_config,
        *,
        repo_name: str = "job",
        timeout: float,
        network: str | None = None,
        mcp_servers: list | None = None,
        extra_args: list[str] | None = None,
    ) -> SandboxRunResult:
        """在容器内跑一次 agent，返回结构化结果（超时则强制杀掉容器）。"""
        if not await self.available():
            raise SandboxUnavailable(f"container runtime '{self.runtime}' is not usable")

        self.work_root.mkdir(parents=True, exist_ok=True)
        run_dir = self.work_root / "runs" / self.container_name(job_id)
        run_dir.mkdir(parents=True, exist_ok=True)
        prompt_path = run_dir / "prompt.txt"
        prompt_path.write_text(prompt, encoding="utf-8")
        config_path = self.write_container_config(
            run_dir / "config.yaml", provider_config, mcp_servers=mcp_servers
        )

        image = await self.ensure_image(repo_name, work_dir)
        name = self.container_name(job_id)
        argv = self.build_run_args(
            name=name,
            image=image,
            work_dir=work_dir,
            shell_command=self.agent_shell_command(),
            config_path=str(config_path),
            prompt_path=str(prompt_path),
            env=self.container_env(provider_config, mcp_servers=mcp_servers),
            network=network,
            extra_args=extra_args,
        )

        log.info("sandbox: starting container %s (image=%s network=%s)", name, image, network or self.config.network)
        await self._evict_stale_container(name)
        code, out = await self._run(argv, timeout=timeout)
        timed_out = code == 124
        if timed_out:
            # 显式收尾：docker run 超时不等于容器停了（仓库已知坑：不能依赖进程退出兜底）
            await self.stop_container(name)
        result = self._parse_output(code, out, timed_out, name)
        if not self.config.keep_containers:
            await self.remove_run_dir(run_dir)
        return result

    async def run_command(
        self,
        job_id: str,
        work_dir: str,
        command: str,
        *,
        repo_name: str = "job",
        timeout: float,
        network: str | None = None,
        mount_src: bool = False,
    ) -> SandboxRunResult:
        """在容器里跑一条仓库命令（测试/验证类），只挂载 worktree。

        ``mount_src=True`` 额外只读挂载 mewcode 源码并设 PYTHONPATH——跑
        mewcode 自身能力（如 MCP server 探活）时用，普通仓库命令不需要。
        """
        if not await self.available():
            raise SandboxUnavailable(f"container runtime '{self.runtime}' is not usable")
        image = await self.ensure_image(repo_name, work_dir)
        name = self.container_name(f"{job_id}-cmd")
        argv = self.build_run_args(
            name=name,
            image=image,
            work_dir=work_dir,
            shell_command=command,
            network=network,
            mount_src=mount_src,
        )
        await self._evict_stale_container(name)
        code, out = await self._run(argv, timeout=timeout)
        if code == 124:
            await self.stop_container(name)
        return SandboxRunResult(
            exit_code=code, stdout=out, timed_out=code == 124, container=name
        )

    def _parse_output(self, code: int, output: str, timed_out: bool, container: str) -> SandboxRunResult:
        """从容器输出里取出最后一行 JSON（agent 的机器可读摘要）。"""
        result = SandboxRunResult(exit_code=code, stdout=output, timed_out=timed_out, container=container)
        for line in reversed(output.strip().splitlines()):
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                payload = json.loads(line)
            except ValueError:
                continue
            if not isinstance(payload, dict) or "result" not in payload:
                continue
            result.result_text = str(payload.get("result", ""))
            usage = payload.get("usage") or {}
            result.input_tokens = int(usage.get("inputTokens", 0) or 0)
            result.output_tokens = int(usage.get("outputTokens", 0) or 0)
            result.tool_calls = int(payload.get("toolCalls", 0) or 0)
            result.extra = {k: v for k, v in payload.items() if k not in ("result", "usage")}
            break
        return result

    async def stop_container(self, name: str) -> None:
        code, out = await self._run([self.runtime, "stop", "-t", str(CONTAINER_STOP_GRACE), name], timeout=60)
        if code != 0 and "No such container" not in out:
            log.warning("sandbox: stop %s failed: %s", name, out.strip()[:200])

    async def remove_container(self, name: str) -> None:
        await self._run([self.runtime, "rm", "-f", name], timeout=60)

    async def _evict_stale_container(self, name: str) -> None:
        """启动同名容器前先清掉残留——收尾必须幂等。

        服务被硬杀（SIGKILL / 掉电 / kill 后台任务）时容器不会走收尾流程，
        而容器名由 job id 决定：重启恢复重跑时 `docker run --name` 会直接
        撞 "name is already in use"，把一个可恢复的 job 变成需人工介入
        （真机演示时踩到：杀掉服务后残留的 agent 容器挡住了下一次运行）。
        容器名这个命名空间归沙箱所有，所以先删后建是安全的；不存在时
        `docker rm -f` 只会报 "No such container"，忽略即可。
        """
        await self.remove_container(name)

    async def remove_run_dir(self, run_dir: Path) -> None:
        await asyncio.to_thread(shutil.rmtree, run_dir, True)
