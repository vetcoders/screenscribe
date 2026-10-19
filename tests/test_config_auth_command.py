"""``screenscribe config auth <provider> --login/--key`` — selection + credentials.

Pins the command surface: exactly one credential mode, a coherent provider
preset write, and the OpenAI account route: api.openai.com rejects ChatGPT
account tokens, so ``openai --login`` (and ``chatgpt``) selects the ChatGPT
Codex backend for LLM + vision and keeps the configured STT.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from screenscribe.cli import app

runner = CliRunner()


@pytest.fixture
def fake_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    return tmp_path


def test_auth_requires_exactly_one_mode(fake_home: Path) -> None:
    assert runner.invoke(app, ["config", "auth", "xai"]).exit_code == 1
    both = runner.invoke(app, ["config", "auth", "xai", "--login", "--key"])
    assert both.exit_code == 1


def test_auth_rejects_unknown_provider(fake_home: Path) -> None:
    result = runner.invoke(app, ["config", "auth", "grok", "--key"], input="x\n")
    assert result.exit_code == 1
    assert "libraxis, openai, chatgpt, xai" in result.output


def test_auth_key_writes_coherent_xai_preset(fake_home: Path) -> None:
    result = runner.invoke(app, ["config", "auth", "xai", "--key"], input="sk-test-xai\ny\n")
    assert result.exit_code == 0, result.output
    config_env = fake_home / ".config" / "screenscribe" / "config.env"
    text = config_env.read_text(encoding="utf-8")
    assert "https://api.x.ai/v1/responses" in text
    assert "https://api.x.ai/v1/stt" in text
    assert "sk-test-xai" in text  # pragma: allowlist secret
    assert "screenscribe doctor" in result.output


def test_auth_key_preserves_existing_config_on_refusal(fake_home: Path) -> None:
    config_env = fake_home / ".config" / "screenscribe" / "config.env"
    config_env.parent.mkdir(parents=True)
    config_env.write_text("SCREENSCRIBE_LANGUAGE=pl\n", encoding="utf-8")
    result = runner.invoke(app, ["config", "auth", "xai", "--key"], input="sk-test-xai\nn\n")
    assert result.exit_code == 0
    assert "api.x.ai" not in config_env.read_text(encoding="utf-8")


def test_auth_login_writes_selection_with_bearer_fallback(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import screenscribe.account_auth as account_auth
    import screenscribe.cli_auth as cli_auth
    from screenscribe.account_auth.device_code import DeviceCode

    monkeypatch.setattr(
        cli_auth,
        "request_device_code",
        lambda provider: DeviceCode(
            provider="xai",
            verification_url="https://example.test/device",
            user_code="ABCD-EFGH",
            device_auth_id="dc",
            interval=0,
        ),
    )
    monkeypatch.setattr(cli_auth, "complete_device_code_login", lambda device: None)
    monkeypatch.setattr(cli_auth.webbrowser, "open", lambda url: True)
    monkeypatch.setattr(
        account_auth,
        "account_status",
        lambda provider: account_auth.AccountStatus(
            provider=provider,
            signed_in=True,
            expires_at=None,
            email="u@example.test",
            api_bearer_usable=True,
            has_refresh_token=True,
        ),
    )

    result = runner.invoke(app, ["config", "auth", "xai", "--login"], input="y\n")
    assert result.exit_code == 0, result.output
    text = (fake_home / ".config" / "screenscribe" / "config.env").read_text(encoding="utf-8")
    assert "https://api.x.ai/v1/responses" in text


def _signed_in_openai(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Stub the device login and report a signed-in OpenAI account; returns login calls."""
    import screenscribe.account_auth as account_auth
    import screenscribe.cli_auth as cli_auth
    from screenscribe.account_auth.device_code import DeviceCode

    logins: list[str] = []

    def request(provider: str) -> DeviceCode:
        logins.append(provider)
        return DeviceCode(provider, "https://example.test/device", "ABCD-EFGH", "dc", 0)

    monkeypatch.setattr(cli_auth, "request_device_code", request)
    monkeypatch.setattr(cli_auth, "complete_device_code_login", lambda device: None)
    monkeypatch.setattr(cli_auth.webbrowser, "open", lambda url: True)
    monkeypatch.setattr(
        account_auth,
        "account_status",
        lambda provider: account_auth.AccountStatus(
            provider=provider,
            signed_in=True,
            expires_at=None,
            email="u@example.test",
            api_bearer_usable=True,
            has_refresh_token=True,
            bearer_hosts=("chatgpt.com",),
        ),
    )
    return logins


@pytest.mark.parametrize("argv", [["openai", "--login"], ["chatgpt"]])
def test_auth_openai_account_selects_chatgpt_route_and_keeps_stt(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch, argv: list[str]
) -> None:
    config_env = fake_home / ".config" / "screenscribe" / "config.env"
    config_env.parent.mkdir(parents=True)
    config_env.write_text(
        "SCREENSCRIBE_STT_ENDPOINT=https://api.x.ai/v1/stt\n"
        "SCREENSCRIBE_STT_API_KEY=xai-stt-fixture\n"  # pragma: allowlist secret
        "SCREENSCRIBE_LLM_ENDPOINT=https://api.x.ai/v1/responses\n"
        "SCREENSCRIBE_VISION_ENDPOINT=https://api.x.ai/v1/responses\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("screenscribe.config.CONFIG_PATHS", [config_env])
    logins = _signed_in_openai(monkeypatch)

    result = runner.invoke(app, ["config", "auth", *argv], input="y\n")
    assert result.exit_code == 0, result.output
    text = config_env.read_text(encoding="utf-8")
    assert "SCREENSCRIBE_PROVIDER=chatgpt" in text
    assert "SCREENSCRIBE_LLM_ENDPOINT=https://chatgpt.com/backend-api/codex/responses" in text
    assert "SCREENSCRIBE_VISION_ENDPOINT=https://chatgpt.com/backend-api/codex/responses" in text
    assert "SCREENSCRIBE_STT_ENDPOINT=https://api.x.ai/v1/stt" in text
    assert "xai-stt-fixture" in text
    # One device login at most: `openai --login` signs in, `chatgpt` reuses the account.
    assert logins == (["openai"] if argv[0] == "openai" else [])


def test_auth_chatgpt_refuses_a_key(fake_home: Path) -> None:
    result = runner.invoke(app, ["config", "auth", "chatgpt", "--key"], input="sk-x\n")
    assert result.exit_code == 1
    assert "not a key" in result.output
    assert not (fake_home / ".config" / "screenscribe" / "config.env").exists()
