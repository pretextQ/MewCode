"""M1 W1: ``service:`` 配置段的加载与校验测试。

凭证（webhook_token / vcs.token）与 api_key 同级对待：
不得出现在 repr 中（traceback 与日志会打印 repr）。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from mewcode.config import ServiceConfig, load_config
from mewcode.validator import ConfigError, validate_service

BASE_CONFIG = (
    "providers:\n"
    "  - name: test\n"
    "    protocol: openai\n"
    "    base_url: http://localhost\n"
    "    model: gpt-4\n"
)


def write_config(tmp_path: Path, extra: str = "") -> Path:
    config_file = tmp_path / "config.yaml"
    config_file.write_text(BASE_CONFIG + extra, encoding="utf-8")
    return config_file


class TestServiceDefaults:
    def test_defaults_when_section_absent(self, tmp_path: Path):
        cfg = load_config(write_config(tmp_path))
        assert cfg.service.port == 8321
        assert cfg.service.host == "127.0.0.1"
        assert cfg.service.concurrency == 3
        assert cfg.service.job_timeout_seconds == 1800
        assert cfg.service.dedup_window_seconds == 1800
        assert cfg.service.notify.type == "none"
        assert cfg.service.vcs.provider == "github"
        assert cfg.service.repos == {}

    def test_dataclass_defaults_match_validator_defaults(self):
        """dataclass 与 validator 的默认值必须一致，否则两条入口会漂移。"""
        from mewcode.validator import validate_service

        v = validate_service(None)
        d = ServiceConfig()
        assert v["port"] == d.port
        assert v["concurrency"] == d.concurrency
        assert v["job_timeout_seconds"] == d.job_timeout_seconds
        assert v["drain_timeout_seconds"] == d.drain_timeout_seconds
        assert v["dedup_window_seconds"] == d.dedup_window_seconds


class TestServiceSection:
    def test_full_section_loads(self, tmp_path: Path):
        cfg = load_config(write_config(tmp_path, (
            "service:\n"
            "  host: 0.0.0.0\n"
            "  port: 9000\n"
            "  concurrency: 5\n"
            "  job_timeout_seconds: 600\n"
            "  drain_timeout_seconds: 30\n"
            "  webhook_token: secret-token\n"
            "  dedup_window_seconds: 60\n"
            "  data_dir: /tmp/mewcode-service\n"
            "  token_budget: 200000\n"
            "  notify:\n"
            "    type: slack\n"
            "    webhook_url: https://hooks.slack.com/services/x\n"
            "  vcs:\n"
            "    provider: github\n"
            "    token: ghp_example\n"
            "    base_branch: main\n"
            "  repos:\n"
            "    demo:\n"
            "      path: /srv/demo\n"
            "      url: https://github.com/acme/demo.git\n"
            "      base_branch: main\n"
        )))
        svc = cfg.service
        assert svc.host == "0.0.0.0"
        assert svc.port == 9000
        assert svc.concurrency == 5
        assert svc.job_timeout_seconds == 600
        assert svc.drain_timeout_seconds == 30
        assert svc.webhook_token == "secret-token"
        assert svc.dedup_window_seconds == 60
        assert svc.data_dir == "/tmp/mewcode-service"
        assert svc.token_budget == 200000
        assert svc.notify.type == "slack"
        assert svc.notify.webhook_url.endswith("/x")
        assert svc.vcs.token == "ghp_example"
        assert svc.vcs.base_branch == "main"
        assert svc.repos["demo"].path == "/srv/demo"
        assert svc.repos["demo"].url == "https://github.com/acme/demo.git"

    def test_unknown_notify_type_rejected(self):
        with pytest.raises(ConfigError, match="notify.type"):
            validate_service({"notify": {"type": "telegram"}})

    def test_unknown_vcs_provider_rejected(self):
        with pytest.raises(ConfigError, match="vcs.provider"):
            validate_service({"vcs": {"provider": "bitbucket"}})

    def test_repo_without_path_rejected(self):
        with pytest.raises(ConfigError, match="must define 'path'"):
            validate_service({"repos": {"demo": {"url": "https://x"}}})

    def test_invalid_port_rejected(self):
        with pytest.raises(ConfigError, match="port"):
            validate_service({"port": 0})
        with pytest.raises(ConfigError, match="port"):
            validate_service({"port": "8321"})
        with pytest.raises(ConfigError, match="port"):
            validate_service({"port": 70000})

    def test_negative_token_budget_rejected(self):
        with pytest.raises(ConfigError, match="token_budget"):
            validate_service({"token_budget": -1})

    def test_zero_token_budget_allowed_as_unlimited(self):
        assert validate_service({"token_budget": 0})["token_budget"] == 0

    def test_non_mapping_service_rejected(self):
        with pytest.raises(ConfigError, match="must be a mapping"):
            validate_service(["nope"])


class TestCredentialHygiene:
    def test_tokens_not_in_repr(self):
        svc = ServiceConfig(webhook_token="super-secret-webhook", vcs=None)  # type: ignore[arg-type]
        assert "super-secret-webhook" not in repr(svc)

    def test_vcs_token_not_in_repr(self):
        from mewcode.config import VCSConfig

        vcs = VCSConfig(token="ghp_topsecret")
        assert "ghp_topsecret" not in repr(vcs)

    def test_loaded_tokens_not_in_repr(self, tmp_path: Path):
        cfg = load_config(write_config(tmp_path, (
            "service:\n"
            "  webhook_token: wh-secret-123\n"
            "  vcs:\n"
            "    token: ghp_secret_456\n"
        )))
        text = repr(cfg.service) + repr(cfg.service.vcs)
        assert "wh-secret-123" not in text
        assert "ghp_secret_456" not in text
