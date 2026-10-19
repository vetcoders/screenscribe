"""OAuth always requires TLS, including overrides and injected clients."""

from pathlib import Path

import httpx
import pytest

from screenscribe import account_auth
from screenscribe.account_auth import AccountAuthError, AccountTokens, refresh_tokens
from screenscribe.account_auth.device_code import (
    DeviceCode,
    _post,
    complete_device_code_login,
    exchange_code_for_tokens,
    request_device_code,
)

MOCK_KEY = "synthetic"  # pragma: allowlist secret -- test token, never a real account


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Never let a refresh test read or write the real account store."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)


@pytest.mark.parametrize("provider", ["openai", "xai"])
@pytest.mark.parametrize(
    "issuer", ["http://example.invalid", "http://localhost:7192", "http://[::1]:7192"]
)
@pytest.mark.parametrize("operation", ["device", "poll", "refresh"])
def test_plaintext_oauth_override_never_sends(
    provider: str, issuer: str, operation: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(f"SCREENSCRIBE_{provider.upper()}_OAUTH_ISSUER", issuer)
    sent: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(500)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(AccountAuthError, match="HTTPS"):
            if operation == "device":
                request_device_code(provider, client=client)
            elif operation == "poll":
                device = DeviceCode(provider, "https://login.example", "synthetic", "device", 1)
                complete_device_code_login(device, client=client)
            else:
                tokens = AccountTokens(access_token=MOCK_KEY, refresh_token=MOCK_KEY)
                refresh_tokens(provider, tokens, client=client)
    assert sent == []


def test_direct_code_exchange_cannot_bypass_issuer_validation() -> None:
    sent: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(500)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(AccountAuthError, match="HTTPS"):
            exchange_code_for_tokens(
                client,
                issuer="http://example.invalid",
                client_id="synthetic",
                redirect_uri="https://login.example/callback",
                code="synthetic",
                code_verifier="synthetic",
            )
    assert sent == []


def test_oauth_error_does_not_echo_issuer_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        "SCREENSCRIBE_XAI_OAUTH_ISSUER",
        "http://user:synthetic@example.invalid/?key=synthetic",  # pragma: allowlist secret -- test URL
    )
    with pytest.raises(AccountAuthError) as error:
        account_auth.issuer_for("xai")
    assert "synthetic" not in str(error.value)
    assert error.value.kind == "http"


@pytest.mark.parametrize("provider", ["openai", "xai"])
@pytest.mark.parametrize("operation", ["device-post", "refresh"])
def test_oauth_rejects_redirect_with_an_injected_redirecting_client(
    provider: str, operation: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv(f"SCREENSCRIBE_{provider.upper()}_OAUTH_ISSUER", "https://issuer.example")
    sent: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(
            307,
            headers={"Location": "http://example.invalid/leak"},
            json={"access_token": "synthetic"},
        )

    with httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True) as client:
        with pytest.raises(AccountAuthError):
            if operation == "device-post":
                _post(client, "https://issuer.example/device", data={"code": "synthetic"})
            else:
                tokens = AccountTokens(access_token=MOCK_KEY, refresh_token=MOCK_KEY)
                refresh_tokens(provider, tokens, client=client)
    assert len(sent) == 1
    assert sent[0].url.scheme == "https"
    assert not account_auth.accounts_path().exists()


def test_generic_supplied_client_cannot_claim_safe_plaintext_routing() -> None:
    from screenscribe.api_utils import UnsafeEndpointError, make_api_request

    sent: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(200)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(UnsafeEndpointError, match="direct loopback routing"):
            make_api_request(
                client, "POST", "http://localhost:7192/voice", json={"text": "synthetic"}
            )
    assert sent == []
