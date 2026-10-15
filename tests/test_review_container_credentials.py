"""Container authentication must match the selected host provider."""
import pytest

from mewcode.config import ProviderConfig, SandboxConfig
from mewcode.service.sandbox import DockerSandbox


@pytest.mark.parametrize("protocol,key_name", [
    ("openai", "OPENAI_API_KEY"), ("openai-compat", "OPENAI_API_KEY"),
    ("anthropic", "ANTHROPIC_API_KEY"),
])
@pytest.mark.parametrize("configured", ["literal-key", "${CUSTOM_PROVIDER_KEY}", "", "${MISSING_KEY}"])
def test_container_respects_provider_key_precedence(tmp_path, monkeypatch, protocol, key_name, configured):
    monkeypatch.setenv(key_name, "standard-key")
    monkeypatch.setenv("CUSTOM_PROVIDER_KEY", "custom-key")
    monkeypatch.delenv("MISSING_KEY", raising=False)
    provider = ProviderConfig("test", protocol, "http://unused", "fake", api_key=configured)
    sandbox = DockerSandbox(SandboxConfig(), mewcode_src=str(tmp_path))
    env = sandbox.container_env(provider)
    assert env[key_name] == provider.resolve_api_key()
    assert "CUSTOM_PROVIDER_KEY" not in env
    config_path = sandbox.write_container_config(tmp_path / "container.yaml", provider)
    content = config_path.read_text(encoding="utf-8")
    assert all(secret not in content for secret in ("literal-key", "standard-key", "custom-key"))
