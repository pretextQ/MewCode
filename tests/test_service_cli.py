"""M1 W1: ``mewcode serve`` CLI 入口行为测试。

关键安全语义：未配置 token 时拒绝监听非回环地址——服务会按告警自动改代码，
裸奔在 0.0.0.0 上等于暴露一个无人值守的 shell。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

from mewcode.__main__ import _is_loopback, _serve_main, main
from mewcode.config import AppConfig, ProviderConfig, ServiceConfig


def make_config(service: ServiceConfig) -> AppConfig:
    return AppConfig(
        providers=[
            ProviderConfig(name="t", protocol="openai", base_url="http://x", model="m")
        ],
        service=service,
    )


class TestLoopbackDetection:
    def test_loopback_hosts(self):
        for host in ("127.0.0.1", "localhost", "::1"):
            assert _is_loopback(host)

    def test_non_loopback_hosts(self):
        for host in ("0.0.0.0", "192.168.1.10", "example.com", ""):
            assert not _is_loopback(host)


class TestServeRefusal:
    def test_binding_non_loopback_without_token_refused(self, monkeypatch, capsys):
        monkeypatch.setattr(
            "mewcode.__main__.load_config",
            lambda: make_config(ServiceConfig(host="0.0.0.0", webhook_token="")),
        )
        with pytest.raises(SystemExit) as ei:
            _serve_main([])
        assert ei.value.code == 2
        err = capsys.readouterr().err
        assert "webhook_token" in err

    def test_binding_non_loopback_with_token_allowed(self, monkeypatch):
        monkeypatch.setattr(
            "mewcode.__main__.load_config",
            lambda: make_config(ServiceConfig(host="0.0.0.0", webhook_token="tok")),
        )
        called: dict = {}

        async def fake_serve(service, host, port, recover=True):
            called.update(host=host, port=port, recover=recover)

        monkeypatch.setattr("mewcode.__main__._serve", fake_serve)
        _serve_main([])
        assert called["host"] == "0.0.0.0"

    def test_cli_overrides_config(self, monkeypatch):
        monkeypatch.setattr(
            "mewcode.__main__.load_config",
            lambda: make_config(ServiceConfig(host="127.0.0.1", port=8321, data_dir="x")),
        )
        called: dict = {}

        async def fake_serve(service, host, port, recover=True):
            called.update(host=host, port=port, recover=recover, data_dir=service.data_dir)

        monkeypatch.setattr("mewcode.__main__._serve", fake_serve)
        _serve_main(["--host", "127.0.0.1", "--port", "9300", "--data-dir", "/tmp/state", "--no-recover"])
        assert called == {"host": "127.0.0.1", "port": 9300, "recover": False, "data_dir": "/tmp/state"}

    def test_config_error_exits_1(self, monkeypatch, capsys):
        from mewcode.config import ConfigError

        def boom():
            raise ConfigError("no config")

        monkeypatch.setattr("mewcode.__main__.load_config", boom)
        with pytest.raises(SystemExit) as ei:
            _serve_main([])
        assert ei.value.code == 1
        assert "no config" in capsys.readouterr().err


class TestServeDispatch:
    def test_serve_subcommand_dispatched(self, monkeypatch):
        captured: dict = {}

        def fake_serve_main(argv):
            captured["argv"] = argv

        monkeypatch.setattr("mewcode.__main__._serve_main", fake_serve_main)
        monkeypatch.setattr(sys, "argv", ["mewcode", "serve", "--port", "9300"])
        main()
        assert captured["argv"] == ["--port", "9300"]

    def test_non_serve_argv_not_dispatched(self, monkeypatch):
        """普通 CLI 调用不得误入 serve 分支。"""
        captured: dict = {}
        monkeypatch.setattr("mewcode.__main__._serve_main", lambda argv: captured.setdefault("hit", True))
        monkeypatch.setattr(sys, "argv", ["mewcode", "--help"])
        with pytest.raises(SystemExit):  # argparse 的 --help 走 SystemExit(0)
            main()
        assert "hit" not in captured


class TestWindowsPortConstraint:
    """本机约束：Windows 保留了 8030-8529（含文档默认 8321），
    因此本地 demo 用 --port 覆盖；此测试记录该事实，避免回归时误判。"""

    def test_default_port_documented(self):
        assert ServiceConfig().port == 8321

    def test_loopback_default(self):
        assert ServiceConfig().host == "127.0.0.1"


class TestModuleEntry:
    def test_main_module_invokes_main(self):
        """`python -m mewcode` 入口仍然指向 main()（serve 分支不破坏原入口）。"""
        import mewcode.__main__ as m

        assert callable(m.main)
        assert Path(m.__file__).name == "__main__.py"


class TestHeadlessOutputFormat:
    """`-p` 的机器可读输出：服务层的沙箱执行靠它回读结果与用量。"""

    def _dispatch(self, monkeypatch, argv: list[str]) -> dict:
        """跑一次 main() 的 -p 分支，捕获 _run_prompt 的调用参数。"""
        captured: dict = {}

        async def fake_run_prompt(config, mode, hooks, prompt, output_format="text"):
            captured.update(prompt=prompt, output_format=output_format)

        monkeypatch.setattr("mewcode.__main__._run_prompt", fake_run_prompt)
        monkeypatch.setattr("mewcode.__main__.load_config", lambda: make_config(ServiceConfig()))
        monkeypatch.setattr("mewcode.__main__.load_hooks", lambda raw: [])
        monkeypatch.setattr(sys, "argv", argv)
        main()
        return captured

    def test_default_is_text(self, monkeypatch):
        assert self._dispatch(monkeypatch, ["mewcode", "-p", "do it"])["output_format"] == "text"

    def test_json_format_selected(self, monkeypatch):
        captured = self._dispatch(monkeypatch, ["mewcode", "-p", "do it", "--output-format", "json"])
        assert captured["output_format"] == "json"
        assert captured["prompt"] == "do it"

    def test_invalid_format_rejected(self, monkeypatch):
        monkeypatch.setattr(sys, "argv", ["mewcode", "-p", "x", "--output-format", "yaml"])
        with pytest.raises(SystemExit):
            main()
