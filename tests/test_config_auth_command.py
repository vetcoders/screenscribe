"""``screenscribe config auth <provider> --login/--key`` — selection + credentials.

Pins the command surface: exactly one credential mode, a coherent provider
preset write, and the OpenAI identity-only refusal (account tokens cannot
serve REST, so --login there refuses instead of writing a dead selection).
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
    assert "libraxis, openai, xai" in result.output


def test_auth_key_writes_coherent_xai_preset(fake_home: Path) -> None:
    result = runner.invoke(
        app, ["config", "auth", "xai", "--key"], input="sk-test-xai\ny\n"
    )
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
    result = runner.invoke(
        app, ["config", "auth", "xai", "--key"], input="sk-test-xai\nn\n"
    )
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
    text = (fake_home / ".config" / "screenscribe" / "config.env").read_text(
        encoding="utf-8"
    )
    assert "https://api.x.ai/v1/responses" in text


def test_auth_login_openai_refuses_identity_only_token(
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
            api_bearer_usable=False,
            has_refresh_token=True,
        ),
    )

    result = runner.invoke(app, ["config", "auth", "openai", "--login"], input="y\n")
    assert result.exit_code == 1
    assert "--key" in result.output
    assert not (fake_home / ".config" / "screenscribe" / "config.env").exists()
