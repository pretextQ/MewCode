
"""MCP 客户端系统的测试（第 6 章）。"""
from __future__ import annotations

import asyncio
import os
import textwrap
import tempfile
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml

from mewcode.config import (
    AppConfig,
    ConfigError,
    MCPServerConfig,
    build_child_env,
    load_config,
    resolve_env_vars,
)

# ===========================================================================
# resolve_env_vars
# ===========================================================================

class TestResolveEnvVars:

    def test_substitutes_existing_var(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MY_TOKEN", "secret123")
        assert resolve_env_vars("${MY_TOKEN}") == "secret123"

    def test_preserves_missing_var(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("NONEXISTENT_VAR", raising=False)
        assert resolve_env_vars("${NONEXISTENT_VAR}") == "${NONEXISTENT_VAR}"

    def test_no_placeholder_passthrough(self) -> None:
        assert resolve_env_vars("plain-text") == "plain-text"

    def test_multiple_vars(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("A", "hello")
        monkeypatch.setenv("B", "world")
        assert resolve_env_vars("${A}-${B}") == "hello-world"

    def test_mixed_existing_and_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("EXISTS", "yes")
        monkeypatch.delenv("NOPE", raising=False)
        assert resolve_env_vars("${EXISTS}/${NOPE}") == "yes/${NOPE}"

# ===========================================================================
# ProviderConfig.resolve_api_key（F3.11）
# ===========================================================================

class TestResolveApiKey:
    def _provider(self, api_key: str = "", protocol: str = "anthropic"):
        from mewcode.config import ProviderConfig

        return ProviderConfig(
            name="test",
            protocol=protocol,
            base_url="http://localhost",
            model="test-model",
            api_key=api_key,
        )

    def test_expands_env_placeholder(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MY_CFG_KEY", "expanded-value")
        provider = self._provider(api_key="${MY_CFG_KEY}")
        assert provider.resolve_api_key() == "expanded-value"

    def test_literal_key_wins(self) -> None:
        provider = self._provider(api_key="literal-key")
        assert provider.resolve_api_key() == "literal-key"

    def test_unresolved_placeholder_falls_back_to_env(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # ${VAR} 未命中时回退协议环境变量，而不是把字面占位符发给 provider。
        monkeypatch.delenv("MISSING_CFG_KEY", raising=False)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "env-fallback-value")
        provider = self._provider(api_key="${MISSING_CFG_KEY}")
        assert provider.resolve_api_key() == "env-fallback-value"

    def test_empty_key_uses_env_map(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "env-only-value")
        provider = self._provider(api_key="")
        assert provider.resolve_api_key() == "env-only-value"

    def test_repr_hides_api_key(self) -> None:
        provider = self._provider(api_key="super-secret-value")
        assert "super-secret-value" not in repr(provider)


# ===========================================================================
# build_child_env
# ===========================================================================

class TestBuildChildEnv:
    def test_includes_path(self) -> None:
        env = build_child_env(None)
        assert "PATH" in env

    def test_includes_declared_vars(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MY_SECRET", "abc")
        env = build_child_env({"TOKEN": "${MY_SECRET}"})
        assert env["TOKEN"] == "abc"
        assert "PATH" in env

    def test_excludes_host_vars(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-secret")
        env = build_child_env({"FOO": "bar"})
        assert "ANTHROPIC_API_KEY" not in env
        assert env["FOO"] == "bar"

    def test_empty_declared_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # F3.11: 白名单继承。清空白名单变量后只剩声明的 env。
        for key in ("PATH", "SystemRoot", "COMSPEC", "TEMP", "TMP", "HOME",
                    "USERPROFILE", "APPDATA", "LOCALAPPDATA", "PROGRAMFILES",
                    "LANG", "LC_ALL"):
            monkeypatch.delenv(key, raising=False)
        env = build_child_env({"FOO": "bar"})
        assert env == {"FOO": "bar"}

    def test_inherits_windows_essentials(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # F3.11: Windows 上缺 SystemRoot/COMSPEC/TEMP 会让 npx 等
        # stdio server 起不来，白名单变量必须继承。
        monkeypatch.setenv("SystemRoot", r"C:\WINDOWS")
        monkeypatch.setenv("COMSPEC", r"C:\WINDOWS\system32\cmd.exe")
        monkeypatch.setenv("TEMP", r"C:\Temp")
        env = build_child_env(None)
        assert env["SystemRoot"] == r"C:\WINDOWS"
        assert env["COMSPEC"] == r"C:\WINDOWS\system32\cmd.exe"
        assert env["TEMP"] == r"C:\Temp"

    def test_does_not_leak_non_allowlisted_vars(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SOME_HOST_SECRET", "leak-me")
        monkeypatch.setenv("DEEPSEEK_API_KEY", "host-secret-value")
        env = build_child_env(None)
        assert "SOME_HOST_SECRET" not in env
        assert "DEEPSEEK_API_KEY" not in env

# ===========================================================================
# load_config：解析 mcp_servers
# ===========================================================================

class TestLoadConfigMCP:
    def _write_config(self, tmp_path: Path, content: str) -> Path:
        p = tmp_path / "config.yaml"
        p.write_text(textwrap.dedent(content))
        return p

    def test_no_mcp_servers(self, tmp_path: Path) -> None:
        path = self._write_config(tmp_path, """\
            providers:
              - name: test
                protocol: openai
                base_url: http://localhost
                model: gpt-4o
        """)
        config = load_config(path)
        assert config.mcp_servers == []

    def test_stdio_server(self, tmp_path: Path) -> None:
        path = self._write_config(tmp_path, """\
            providers:
              - name: test
                protocol: openai
                base_url: http://localhost
                model: gpt-4o
            mcp_servers:
              - name: github
                command: npx
                args: ["-y", "@modelcontextprotocol/server-github"]
                env:
                  GITHUB_TOKEN: "${GITHUB_TOKEN}"
        """)
        config = load_config(path)
        assert len(config.mcp_servers) == 1
        srv = config.mcp_servers[0]
        assert srv.name == "github"
        assert srv.command == "npx"
        assert srv.is_stdio is True
        assert srv.args == ["-y", "@modelcontextprotocol/server-github"]

    def test_http_server(self, tmp_path: Path) -> None:
        path = self._write_config(tmp_path, """\
            providers:
              - name: test
                protocol: openai
                base_url: http://localhost
                model: gpt-4o
            mcp_servers:
              - name: remote
                url: "https://api.example.com/mcp"
                headers:
                  Authorization: "Bearer ${TOKEN}"
        """)
        config = load_config(path)
        srv = config.mcp_servers[0]
        assert srv.name == "remote"
        assert srv.url == "https://api.example.com/mcp"
        assert srv.is_stdio is False
        # F3.11: 未显式声明 transport 时按 url 推断为 http。
        assert srv.transport == "http"

    def test_stdio_server_infers_stdio_transport(self, tmp_path: Path) -> None:
        # F3.11: command-only 配置推断为 stdio（旧行为兼容）。
        path = self._write_config(tmp_path, """\
            providers:
              - name: test
                protocol: openai
                base_url: http://localhost
                model: gpt-4o
            mcp_servers:
              - name: local
                command: npx
                args: ["-y", "@modelcontextprotocol/server-github"]
        """)
        config = load_config(path)
        srv = config.mcp_servers[0]
        assert srv.transport == "stdio"
        assert srv.is_stdio is True

    def test_explicit_http_transport(self, tmp_path: Path) -> None:
        path = self._write_config(tmp_path, """\
            providers:
              - name: test
                protocol: openai
                base_url: http://localhost
                model: gpt-4o
            mcp_servers:
              - name: remote
                url: "https://api.example.com/mcp"
                transport: http
        """)
        config = load_config(path)
        srv = config.mcp_servers[0]
        assert srv.transport == "http"
        assert srv.is_stdio is False

    def test_invalid_transport_errors(self, tmp_path: Path) -> None:
        path = self._write_config(tmp_path, """\
            providers:
              - name: test
                protocol: openai
                base_url: http://localhost
                model: gpt-4o
            mcp_servers:
              - name: remote
                url: "https://api.example.com/mcp"
                transport: websocket
        """)
        with pytest.raises(ConfigError, match="invalid transport 'websocket'"):
            load_config(path)

    def test_http_transport_requires_url(self, tmp_path: Path) -> None:
        path = self._write_config(tmp_path, """\
            providers:
              - name: test
                protocol: openai
                base_url: http://localhost
                model: gpt-4o
            mcp_servers:
              - name: remote
                command: npx
                transport: http
        """)
        with pytest.raises(ConfigError, match="transport 'http' requires 'url'"):
            load_config(path)

    def test_stdio_transport_requires_command(self, tmp_path: Path) -> None:
        path = self._write_config(tmp_path, """\
            providers:
              - name: test
                protocol: openai
                base_url: http://localhost
                model: gpt-4o
            mcp_servers:
              - name: remote
                url: "https://api.example.com/mcp"
                transport: stdio
        """)
        with pytest.raises(
            ConfigError, match="transport 'stdio' requires 'command'"
        ):
            load_config(path)

    def test_both_command_and_url_errors(self, tmp_path: Path) -> None:
        path = self._write_config(tmp_path, """\
            providers:
              - name: test
                protocol: openai
                base_url: http://localhost
                model: gpt-4o
            mcp_servers:
              - name: bad
                command: npx
                url: "https://example.com"
        """)
        with pytest.raises(ConfigError, match="cannot have both"):
            load_config(path)

    def test_neither_command_nor_url_errors(self, tmp_path: Path) -> None:
        path = self._write_config(tmp_path, """\
            providers:
              - name: test
                protocol: openai
                base_url: http://localhost
                model: gpt-4o
            mcp_servers:
              - name: bad
                env:
                  FOO: bar
        """)
        with pytest.raises(ConfigError, match="must have either"):
            load_config(path)

# ===========================================================================
# MCPToolWrapper
# ===========================================================================

class TestMCPToolWrapper:
    def test_name_format(self) -> None:
        from mcp import types as mcp_types
        from mewcode.mcp.tool_wrapper import MCPToolWrapper
        from mewcode.mcp.client import MCPClient

        tool_def = mcp_types.Tool(
            name="search_issues",
            description="Search GitHub issues",
            inputSchema={
                "type": "object",
                "properties": {
                    "repo": {"type": "string"},
                    "query": {"type": "string"},
                },
                "required": ["repo"],
            },
        )
        mock_client = MagicMock(spec=MCPClient)
        wrapper = MCPToolWrapper("github", tool_def, mock_client)

        assert wrapper.name == "mcp_github_search_issues"
        assert wrapper.category == "command"
        assert wrapper.description == "Search GitHub issues"

    def test_get_schema_uses_original_input_schema(self) -> None:
        from mcp import types as mcp_types
        from mewcode.mcp.tool_wrapper import MCPToolWrapper

        input_schema = {
            "type": "object",
            "properties": {"q": {"type": "string"}},
            "required": ["q"],
        }
        tool_def = mcp_types.Tool(
            name="search",
            description="Search",
            inputSchema=input_schema,
        )
        mock_client = MagicMock()
        wrapper = MCPToolWrapper("srv", tool_def, mock_client)

        schema = wrapper.get_schema()
        assert schema["name"] == "mcp_srv_search"
        assert schema["input_schema"] == input_schema

# ===========================================================================
# _extract_text
# ===========================================================================

class TestExtractText:
    def test_text_content(self) -> None:
        from mcp import types as mcp_types
        from mewcode.mcp.tool_wrapper import _extract_text

        content = [
            mcp_types.TextContent(type="text", text="hello"),
            mcp_types.TextContent(type="text", text="world"),
        ]
        assert _extract_text(content) == "hello\nworld"

    def test_empty_content(self) -> None:
        from mewcode.mcp.tool_wrapper import _extract_text

        assert _extract_text([]) == "(no output)"

    def test_image_content(self) -> None:
        from mcp import types as mcp_types
        from mewcode.mcp.tool_wrapper import _extract_text

        content = [mcp_types.ImageContent(type="image", data="...", mimeType="image/png")]
        assert "[image: image/png]" in _extract_text(content)

# ===========================================================================
# MCPManager：部分失败容错
# ===========================================================================

class TestMCPManagerPartialFailure:
    @pytest.mark.asyncio
    async def test_single_server_failure_does_not_block_others(self) -> None:
        from mewcode.mcp.manager import MCPManager
        from mewcode.tools import ToolRegistry

        good_config = MCPServerConfig(
            name="good",
            command="echo",
            args=["hello"],
        )
        bad_config = MCPServerConfig(
            name="bad",
            command="nonexistent_command_xyz_12345",
        )

        manager = MCPManager()
        manager.load_configs([bad_config, good_config])

        registry = ToolRegistry()

        with patch("mewcode.mcp.manager.MCPClient") as MockClient:
            good_instance = AsyncMock()
            good_instance.is_alive = True

            from mcp import types as mcp_types
            good_instance.list_tools.return_value = [
                mcp_types.Tool(
                    name="test_tool",
                    description="A test",
                    inputSchema={"type": "object", "properties": {}},
                )
            ]

            bad_instance = AsyncMock()
            bad_instance.connect.side_effect = RuntimeError("command not found")

            def make_client(config: MCPServerConfig) -> AsyncMock:
                if config.name == "bad":
                    return bad_instance
                return good_instance

            MockClient.side_effect = make_client

            errors = await manager.register_all_tools(registry)

        assert len(errors) == 1
        assert "bad" in errors[0]
        assert registry.get("mcp_good_test_tool") is not None
