from __future__ import annotations

import hashlib
import importlib.resources
import logging
from pathlib import Path

from mewcode.agents.parser import AgentDef, AgentParseError, parse_agent_file

log = logging.getLogger(__name__)

PROJECT_AGENTS_DIR = ".mewcode/agents"
USER_AGENTS_DIR = "~/.mewcode/agents"


class AgentLoader:


    def __init__(
        self,
        work_dir: str,
        enable_verification: bool = False,
    ) -> None:
        self._work_dir = work_dir
        self._enable_verification = enable_verification
        self._agents: dict[str, AgentDef] = {}
        self._file_cache: dict[Path, tuple[tuple[int, int, str], AgentDef]] = {}
        self._directory_signature: tuple = ()
        self._plugin_sources: list[Path] = []


    def _scan_directory(self, path: Path, source: str) -> list[AgentDef]:
        results: list[AgentDef] = []
        if not path.is_dir():
            return results

        for entry in sorted(path.iterdir()):
            if not entry.is_file() or entry.suffix != ".md":
                continue
            try:
                stat = entry.stat()
                signature = (stat.st_mtime_ns, stat.st_size, hashlib.sha256(entry.read_bytes()).hexdigest())
                cached = self._file_cache.get(entry)
                if cached and cached[0] == signature:
                    agent_def = cached[1]
                else:
                    agent_def = parse_agent_file(entry)
                    agent_def.source = source
                    agent_def.file_path = entry
                    self._file_cache[entry] = (signature, agent_def)
                results.append(agent_def)
            except (AgentParseError, OSError) as e:
                log.warning("Skipping agent file %s: %s", entry, e)
        return results


    def _load_builtins(self) -> list[AgentDef]:
        results: list[AgentDef] = []
        try:
            builtins_pkg = importlib.resources.files("mewcode.agents.builtins")
        except (ModuleNotFoundError, TypeError):
            log.warning("Could not load built-in agents package")
            return results

        for item in builtins_pkg.iterdir():
            if not item.name.endswith(".md"):
                continue
            try:
                raw = item.read_text(encoding="utf-8")
                from mewcode.agents.parser import _validate_agent_meta, parse_frontmatter

                meta, body = parse_frontmatter(raw)
                _validate_agent_meta(meta, item.name)

                agent_def = AgentDef(
                    agent_type=meta["name"],
                    when_to_use=meta["description"],
                    system_prompt=body,
                    tools=meta.get("tools", []),
                    disallowed_tools=meta.get("disallowedTools", []),
                    model=str(meta.get("model", "inherit")),
                    max_turns=meta.get("maxTurns", 50),
                    permission_mode=str(meta.get("permissionMode", "default")),
                    background=bool(meta.get("background", False)),
                    file_path=None,
                    source="builtin",
                )

                if (
                    agent_def.agent_type == "Verification"
                    and not self._enable_verification
                ):
                    continue

                results.append(agent_def)
            except (AgentParseError, Exception) as e:
                log.warning("Skipping built-in agent %s: %s", item.name, e)

        return results

    def load_all(self) -> dict[str, AgentDef]:
        seen: dict[str, AgentDef] = {}

        # 优先级 1：项目级（最高）
        project_path = Path(self._work_dir) / PROJECT_AGENTS_DIR
        for agent_def in self._scan_directory(project_path, "project"):
            if agent_def.agent_type not in seen:
                seen[agent_def.agent_type] = agent_def

        # 优先级 2：用户级
        user_path = Path(USER_AGENTS_DIR).expanduser()
        for agent_def in self._scan_directory(user_path, "user"):
            if agent_def.agent_type not in seen:
                seen[agent_def.agent_type] = agent_def

        # 优先级 3：内置
        for agent_def in self._load_builtins():
            if agent_def.agent_type not in seen:
                seen[agent_def.agent_type] = agent_def

        # 插件不覆盖项目、用户或内置定义。
        for path in self._plugin_sources:
            for agent_def in self._scan_directory(path, "plugin"):
                seen.setdefault(agent_def.agent_type, agent_def)

        self._agents = seen
        self._directory_signature = self._directories_state()
        return seen


    def _directories_state(self) -> tuple:
        signatures = []
        for path in (
            Path(self._work_dir) / PROJECT_AGENTS_DIR, Path(USER_AGENTS_DIR).expanduser(), *self._plugin_sources,
        ):
            try:
                signatures.append((str(path), tuple(sorted(p.name for p in path.glob("*.md")))))
            except OSError:
                signatures.append((str(path), None))
        return tuple(signatures)

    def _refresh_catalog(self) -> None:
        if self._directories_state() != self._directory_signature:
            self.load_all()

    def get(self, agent_type: str) -> AgentDef | None:
        self._refresh_catalog()
        cached = self._agents.get(agent_type)
        if cached is None or cached.file_path is None:
            return cached
        path = cached.file_path
        try:
            stat = path.stat()
            entry = self._file_cache.get(path)
            signature = (stat.st_mtime_ns, stat.st_size, hashlib.sha256(path.read_bytes()).hexdigest())
            if entry and entry[0] == signature:
                return entry[1]
            reloaded = parse_agent_file(path)
            reloaded.source = cached.source
            reloaded.file_path = path
            self._file_cache[path] = (signature, reloaded)
            self._agents[agent_type] = reloaded
            return reloaded
        except (OSError, AgentParseError) as e:
            log.warning("Hot reload failed for %s: %s", agent_type, e)
            return cached

    def list_agents(self) -> list[tuple[str, str]]:
        self._refresh_catalog()
        return [(ad.agent_type, ad.when_to_use) for ad in self._agents.values()]

    def register_plugin_source(self, path: Path) -> None:
        path = path.resolve()
        if not path.is_dir():
            raise ValueError(f"agent plugin source is not a directory: {path}")
        if path not in self._plugin_sources:
            self._plugin_sources.append(path)
            self.load_all()
