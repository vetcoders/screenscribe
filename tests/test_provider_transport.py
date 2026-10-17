"""Provider transport and identity boundaries; only synthetic data, no remote requests."""

import asyncio
import importlib
import json
from typing import Any

import httpx
import pytest
from websockets.datastructures import Headers
from websockets.exceptions import InvalidStatus
from websockets.http11 import Response

from screenscribe import stt_stream, transcribe
from screenscribe.agent import chat
from screenscribe.api_utils import UnsafeEndpointError, make_api_request, validate_provider_endpoint
from screenscribe.config import ScreenScribeConfig
from screenscribe.providers.xai import list_voices_xai, synthesize_speech_xai, transcribe_file_xai

MOCK_API_KEY = "test"  # pragma: allowlist secret -- synthetic, never sent to a provider


@pytest.mark.parametrize(
    "url",
    [
        "https://custom.example/api/v1/responses",
        "http://localhost:7237/transcribe",
        "http://LOCALHOST.:7237/transcribe",
        "http://127.0.0.1:7237/transcribe",
        "http://127.0.0.2:7237/transcribe",
        "http://[::1]:7237/transcribe",
        "http://[::ffff:127.0.0.1]:7237/transcribe",
    ],
)
def test_tls_and_strict_loopback_are_supported(url: str) -> None:
    validate_provider_endpoint(url)
    config = ScreenScribeConfig.provider_preset("custom", MOCK_API_KEY, custom_base=url)
    assert not config.validate()


@pytest.mark.parametrize(
    "url",
    [
        "http://example.invalid/v1/responses",
        "http://localhost.example.invalid/v1/responses",
        "http://127.0.0.1.example.invalid/v1/responses",
        "http://localhost@example.invalid/v1/responses",
        "http://192.168.1.2/v1/responses",
        "http://2130706433/v1/responses",
        "http://0177.0.0.1/v1/responses",
        "http://[::ffff:192.168.1.2]/v1/responses",
        "ftp://localhost/v1/responses",
        "https:///missing-host",
        "https://localhost:bad/v1/responses",
        "https://local\nhost/v1/responses",
        "http://localhost\\@example.invalid/v1/responses",
    ],
)
def test_invalid_or_cleartext_remote_destinations_are_rejected(url: str) -> None:
    with pytest.raises(UnsafeEndpointError):
        validate_provider_endpoint(url)
    with pytest.raises(UnsafeEndpointError):
        ScreenScribeConfig.provider_preset("custom", MOCK_API_KEY, custom_base=url)


@pytest.mark.parametrize(
    "field",
    [
        "stt_endpoint",
        "llm_endpoint",
        "vision_endpoint",
        "stt_fallback_endpoint",
        "tts_endpoint",
        "stt_live_endpoint",
    ],
)
def test_config_validation_covers_endpoint_overrides(field: str) -> None:
    config = ScreenScribeConfig(api_key=MOCK_API_KEY)
    scheme = "ws" if field == "stt_live_endpoint" else "http"
    setattr(config, field, f"{scheme}://example.invalid/provider")
    assert any("require" in error for error in config.validate())


def test_scoped_stt_validation_does_not_block_on_unused_tts_or_live_endpoint() -> None:
    config = ScreenScribeConfig.provider_preset("xai", MOCK_API_KEY)
    config.tts_endpoint = "http://example.invalid/tts"
    config.stt_live_endpoint = "ws://example.invalid/live"
    assert config.validate({"stt"}) == []
    assert len(config.validate()) == 2


def test_transport_error_does_not_echo_url_credentials() -> None:
    with pytest.raises(UnsafeEndpointError) as error:
        validate_provider_endpoint(
            "http://user:private-value@example.invalid/?key=another-value"  # pragma: allowlist secret -- synthetic redaction canary
        )
    assert "private-value" not in str(error.value)
    assert "another-value" not in str(error.value)


@pytest.mark.parametrize(
    "host",
    [
        "libraxis.cloud",
        "unlisted.libraxis.cloud",
        "api.libraxis.cloud.example.invalid",
        "api.openai.com.example.invalid",
    ],
)
def test_unlisted_host_does_not_gain_provider_identity_or_implicit_internal_trust(
    host: str,
) -> None:
    config = ScreenScribeConfig(api_key=MOCK_API_KEY)
    config._libraxis_key_values.add(MOCK_API_KEY)
    config.llm_endpoint = f"https://{host}/v1/responses"
    assert config._endpoint_provider(config.llm_endpoint) is None
    assert config.get_llm_api_key() == ""
    assert chat.infer_trust(host) == "external"


@pytest.mark.parametrize(
    "host", ["api.libraxis.cloud", "API.LIBRAXIS.CLOUD.", "api.libraxis\u3002cloud"]
)
def test_exact_internal_host_canonicalizes_consistently(host: str) -> None:
    assert ScreenScribeConfig._endpoint_provider(f"https://{host}/v1/responses") == "libraxis"
    assert chat.infer_trust(host) == "internal"
    assert chat._host_of(f"https://{host}/v1/responses") == "api.libraxis.cloud"


@pytest.mark.parametrize(
    "host,expected_xai",
    [
        ("api.x.ai", True),
        ("API.X.AI.", True),
        ("api.x\u3002ai", True),
        ("x.ai", False),
        ("unlisted.x.ai", False),
        ("api.x.ai.example.invalid", False),
    ],
)
def test_stt_dispatch_agrees_with_exact_provider_identity(host: str, expected_xai: bool) -> None:
    endpoint = f"https://{host}/v1/stt"
    assert (ScreenScribeConfig._endpoint_provider(endpoint) == "xai") is expected_xai
    assert transcribe._is_xai_endpoint(endpoint) is expected_xai
    client = stt_stream.stream_client_for_endpoint(f"wss://{host}/v1/stt", api_key=MOCK_API_KEY)
    assert isinstance(client, stt_stream.XaiStreamClient) is expected_xai


@pytest.mark.parametrize("operation", ["stt", "tts", "voices"])
def test_direct_xai_calls_block_before_transport(operation: str) -> None:
    sent: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(200, json={})

    kwargs: dict[str, Any] = {
        "api_key": MOCK_API_KEY,
        "endpoint": "http://example.invalid/v1/stt",
        "transport": httpx.MockTransport(handler),
    }
    with pytest.raises(UnsafeEndpointError):
        if operation == "stt":
            transcribe_file_xai(b"synthetic-audio", "example.wav", language="en", **kwargs)
        elif operation == "tts":
            synthesize_speech_xai("synthetic text", language="en", **kwargs)
        else:
            list_voices_xai(**kwargs)
    assert sent == []


@pytest.mark.parametrize(
    "endpoint", ["http://example.invalid/stt", "http://localhost.example.invalid/stt"]
)
def test_direct_transcription_blocks_media_upload(
    monkeypatch: pytest.MonkeyPatch, endpoint: str
) -> None:
    sent: list[httpx.Request] = []
    real_client = httpx.Client

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(200, json={"text": "unexpected"})

    monkeypatch.setattr(
        httpx, "Client", lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw)
    )
    with pytest.raises(UnsafeEndpointError):
        transcribe._request_transcription_payload(
            b"synthetic-audio",
            "example.wav",
            api_key=MOCK_API_KEY,
            stt_endpoint=endpoint,
        )
    assert sent == []


@pytest.mark.parametrize("protocol", ["responses", "anthropic"])
def test_agent_fallback_cannot_bypass_transport_policy(
    protocol: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_send(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("an insecure provider reached its transport")

    monkeypatch.setattr(httpx.AsyncClient, "stream", no_send)
    provider = chat.AgentProvider(
        name="fallback",
        protocol=protocol,
        key=MOCK_API_KEY,
        model="test",
        url="http://example.invalid/v1/messages",
        trust="internal",
        slot="fallback",
        host="example.invalid",
    )
    call = chat._responses_round if protocol == "responses" else chat._anthropic_round
    with pytest.raises(UnsafeEndpointError):
        asyncio.run(call(provider, {"input": []}))


def test_live_stt_rechecks_mutated_url_before_connect(monkeypatch: pytest.MonkeyPatch) -> None:
    def no_connect(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("an insecure websocket reached connect")

    client = stt_stream.stream_client_for_endpoint("ws://localhost:7237/live", api_key=MOCK_API_KEY)
    client.url = "ws://example.invalid/live"
    events: list[Any] = []
    monkeypatch.setattr(client, "_emit", events.append)
    monkeypatch.setattr(stt_stream, "DirectProviderConnect", no_connect)
    asyncio.run(client._run())
    assert len(events) == 1 and events[0].kind == "error"
    assert "require WSS" in events[0].text


def test_live_stt_rejects_redirect_to_an_unchecked_destination() -> None:
    error = InvalidStatus(
        Response(302, "Found", Headers({"Location": "ws://example.invalid/live"}))
    )

    async def check() -> None:
        connection = stt_stream.DirectProviderConnect("ws://127.0.0.1:7237/live")
        # An exception means the library aborts instead of following a returned URI.
        assert isinstance(connection.process_redirect(error), Exception)

    asyncio.run(check())


def test_request_helper_does_not_follow_redirects_even_with_redirecting_client() -> None:
    sent: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(str(request.url))
        return httpx.Response(302, headers={"Location": "http://example.invalid/redirected"})

    with httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True) as client:
        with pytest.raises(httpx.HTTPStatusError):
            make_api_request(
                client, "POST", "https://example.invalid/provider", json={"text": "synthetic"}
            )
    assert sent == ["https://example.invalid/provider"]


def test_processing_receipt_uses_the_same_canonical_host() -> None:
    config = ScreenScribeConfig.provider_preset("xai", MOCK_API_KEY)
    hosts = chat.analysis_hosts(
        config, {"llm": {"host": "API.X.AI.", "provider": "xai", "protocol": "responses"}}
    )
    assert hosts == frozenset({"api.x.ai"})
    assert chat.infer_trust("api.x.ai", processor_hosts=hosts) == "processor"


@pytest.mark.parametrize("redirect", [False, True])
def test_real_anthropic_sdk_stream_and_redirect_boundary(
    monkeypatch: pytest.MonkeyPatch, redirect: bool
) -> None:
    anthropic = pytest.importorskip("anthropic")
    client_type = anthropic.DefaultAsyncHttpxClient
    # The pinned SDK uses httpx2; older supported extras use httpx. Build the
    # mock with the same public transport types as the installed SDK client.
    native_module = next(
        base.__module__.split(".")[0]
        for base in client_type.__mro__
        if base.__module__.split(".")[0] in {"httpx", "httpx2"}
    )
    native_http = importlib.import_module(native_module)
    sent: list[str] = []
    clients: list[Any] = []
    events = [
        {
            "type": "message_start",
            "message": {
                "id": "msg_test",
                "type": "message",
                "role": "assistant",
                "model": "test",
                "content": [],
                "stop_reason": None,
                "stop_sequence": None,
                "usage": {"input_tokens": 1, "output_tokens": 0},
            },
        },
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "text_delta", "text": "synthetic reply"},
        },
        {"type": "content_block_stop", "index": 0},
        {
            "type": "message_delta",
            "delta": {"stop_reason": "end_turn", "stop_sequence": None},
            "usage": {"output_tokens": 1},
        },
        {"type": "message_stop"},
    ]
    body = "".join(f"event: {event['type']}\ndata: {json.dumps(event)}\n\n" for event in events)

    def handler(request: Any) -> Any:
        sent.append(str(request.url))
        if redirect:
            return native_http.Response(302, headers={"Location": "http://example.invalid/leak"})
        return native_http.Response(
            200, headers={"Content-Type": "text/event-stream"}, content=body.encode()
        )

    def factory(**kwargs: Any) -> Any:
        client = client_type(transport=native_http.MockTransport(handler), **kwargs)
        clients.append(client)
        return client

    monkeypatch.setattr(anthropic, "DefaultAsyncHttpxClient", factory)
    provider = chat.AgentProvider(
        name="fallback",
        protocol="anthropic",
        key=MOCK_API_KEY,
        model="test",
        url="https://example.invalid/custom/v1/messages",
        trust="external",
        slot="fallback",
        host="example.invalid",
    )
    if redirect:
        with pytest.raises(anthropic.APIStatusError):
            asyncio.run(chat._anthropic_round(provider, {"input": []}))
    else:
        result = asyncio.run(chat._anthropic_round(provider, {"input": []}))
        assert result.text == "synthetic reply"
    assert sent == ["https://example.invalid/custom/v1/messages"]
    assert clients[0].is_closed
    assert clients[0].follow_redirects is False
