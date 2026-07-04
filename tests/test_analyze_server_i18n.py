"""Tests for the analyze dashboard PL/EN i18n toggle (Group D, Issue #9).

The dashboard ships with a small inline i18n dict and a render-time DOM
walker that swaps text/attributes based on a `data-i18n*` attribute. The
toggle also propagates the chosen language to the analyze endpoints
(`/api/analyze/{id}`, `/api/finalize/start`, `/api/finalize`) so the
VLM produces findings in the matching language.

These tests assert two things:

1. The HTML contains both the EN and PL strings (so the i18n dict is
   actually wired in) and the toggle widget itself.
2. The analyze endpoint forwards `lang` from the request body to the
   underlying `analyze_finding_unified` call via
   ``dataclasses.replace(config, language=...)``.

The VLM call is monkey-patched so the test never reaches the network and
so we can assert the language string the analyze code path picked.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from screenscribe.analyze_server import create_analyze_app
from screenscribe.config import ScreenScribeConfig
from screenscribe.unified_analysis import UnifiedFinding

PNG_1X1_BASE64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk"
    "+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)


@pytest.fixture
def sample_video(tmp_path: Path) -> Path:
    video_path = tmp_path / "demo.mp4"
    video_path.write_bytes(b"\x00\x00\x00\x14ftypmp42\x00\x00\x00\x00mp42")
    return video_path


def _config(language: str = "en") -> ScreenScribeConfig:
    return ScreenScribeConfig(
        api_key="test-key",  # pragma: allowlist secret
        vision_api_key="test-key",  # pragma: allowlist secret
        stt_endpoint="https://api.example.com/v1/audio/transcriptions",
        stt_model="test-model",
        language=language,
    )


def _mark(client: TestClient, *, timestamp: float = 1.0) -> str:
    response = client.post(
        "/api/mark",
        json={
            "timestamp": timestamp,
            "frame_base64": PNG_1X1_BASE64,
            "transcript": "",
            "notes": "",
        },
    )
    assert response.status_code == 200
    return str(response.json()["marker_id"])


def _stub_finding(category: str = "user_marked") -> UnifiedFinding:
    return UnifiedFinding(
        detection_id=1,
        screenshot_path=Path("stub.png"),
        timestamp=0.0,
        category=category,
        is_issue=True,
        sentiment="problem",
        severity="medium",
        summary="stub",
        action_items=[],
        affected_components=[],
        suggested_fix="",
        ui_elements=[],
        issues_detected=[],
        accessibility_notes=[],
        design_feedback="",
        technical_observations="",
        response_id="resp-stub",
    )


def test_dashboard_includes_lang_toggle_widget(sample_video: Path) -> None:
    """The dashboard header must expose a clickable PL/EN toggle.

    This is the visible affordance for Issue #9. Without it the i18n dict
    is unreachable from the UI even if it ships in the HTML.
    """
    app = create_analyze_app(sample_video, _config())
    client = TestClient(app)

    response = client.get("/")
    assert response.status_code == 200
    html = response.text

    # Toggle container + the two language buttons.
    assert 'id="langToggle"' in html
    assert 'data-lang="en"' in html
    assert 'data-lang="pl"' in html
    # localStorage persistence key — reload should keep the chosen language.
    assert "screenscribe_analyze_lang" in html


def test_dashboard_ships_both_language_dictionaries(sample_video: Path) -> None:
    """The inline i18n dict must contain both EN and PL translations.

    Picks one EN string and its PL counterpart that have to coexist in the
    rendered page. Also asserts the UX hint copy from Issue #11 in both
    languages.
    """
    app = create_analyze_app(sample_video, _config())
    client = TestClient(app)

    response = client.get("/")
    html = response.text

    # Status / button labels in both languages.
    assert "Ready" in html
    assert "Gotowe" in html
    assert "Add moment" in html
    assert "Dodaj moment" in html
    assert "Momenty" in html  # unified moments tab (was "Uwagi")
    assert "Analizuj" in html  # PL action label
    assert "Re-analyze" in html
    assert "Analizuj ponownie" in html

    # UX hint (Issue #11; A3 reframed to a flexible, non-prescriptive flow:
    # mark a moment first, notes are optional and editable later).
    assert (
        "Pause the video and mark a moment. Add a voice or text note now or later"
        " — notes are optional" in html
    )
    assert (
        "Zatrzymaj film i oznacz moment. Notatkę głosową lub tekstową dodasz teraz"
        " lub później — jest opcjonalna" in html
    )
    # The old forced-order phrasing ("...then add a moment") must be gone.
    assert "then add a moment" not in html
    assert "a potem dodaj moment" not in html

    # Bucket B/C — human-centered panel copy in both languages.
    assert "Mark the moment" in html
    assert "Oznacz ważny moment" in html
    assert "Record a voice note" in html
    assert "Nagraj notatkę głosową" in html
    assert "UI:" in html
    assert "Interfejs:" in html
    assert "Eksport" in html  # export tab (PL unambiguous; EN covered structurally)
    assert "How it works" in html
    assert "Jak to działa" in html
    assert "Review and export" in html
    assert "Przejrzyj i wyeksportuj raport" in html


def test_dashboard_i18n_templates_match_tformat_syntax(sample_video: Path) -> None:
    """Templated i18n strings must use the ``{{name}}`` syntax tFormat replaces."""
    app = create_analyze_app(sample_video, _config())
    client = TestClient(app)

    html = client.get("/").text

    assert "Finalizing... {{processed}}/{{total}}" in html
    assert "Report ready: {{completed}} completed, {{errors}} errors" in html
    assert "{{n}} errors" in html
    assert "Finalizing... {processed}/{total}" not in html
    assert "Report ready: {completed} completed, {errors} errors" not in html
    assert 'errors_count: "{n} errors"' not in html

    # Reactive video-status templates use the same double-brace {{time}} form.
    assert "Paused at {{time}} — you can add a moment" in html
    assert "Zatrzymane na {{time}} — możesz dodać moment" in html
    assert "Paused at {time} — you can add a moment" not in html


def test_dashboard_default_lang_follows_config_language(sample_video: Path) -> None:
    """`<body data-default-lang>` mirrors the CLI --lang config value."""
    app_pl = create_analyze_app(sample_video, _config(language="pl"))
    client_pl = TestClient(app_pl)
    html_pl = client_pl.get("/").text
    assert 'data-default-lang="pl"' in html_pl

    app_en = create_analyze_app(sample_video, _config(language="en"))
    client_en = TestClient(app_en)
    html_en = client_en.get("/").text
    assert 'data-default-lang="en"' in html_en


def test_dashboard_displays_spoken_language_contract(sample_video: Path) -> None:
    """UI/VLM toggle is separate from the spoken language used by STT."""
    app = create_analyze_app(sample_video, _config(language="pl"))
    client = TestClient(app)

    html = client.get("/").text

    assert 'data-speech-lang="pl"' in html
    assert 'id="speechLanguageValue">PL</strong>' in html
    assert "Speech:" in html
    assert "Mowa:" in html


def test_analyze_endpoint_uses_lang_from_request_body(
    sample_video: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """POST /api/analyze/{id} with ``{"lang":"pl"}`` runs the VLM in PL.

    Verifies the wiring: dashboard sends the toggle state in the body,
    server pulls it out, builds ``dataclasses.replace(config, language=pl)``
    and the VLM call sees that config (we assert via the patched call).
    """
    app = create_analyze_app(sample_video, _config(language="en"))
    client = TestClient(app)
    marker_id = _mark(client)

    captured: dict[str, Any] = {}

    def fake_analyze(detection: Any, screenshot_path: Any, config: Any, **_: Any) -> UnifiedFinding:
        captured["language"] = config.language
        return _stub_finding()

    monkeypatch.setattr(
        "screenscribe.unified_analysis.analyze_finding_unified",
        fake_analyze,
    )

    response = client.post(f"/api/analyze/{marker_id}", json={"lang": "pl"})

    assert response.status_code == 200
    assert captured["language"] == "pl"


def test_analyze_endpoint_falls_back_to_config_language_when_body_missing(
    sample_video: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No body == use ``config.language`` (the CLI ``--lang`` value).

    Default config is EN here, so we expect EN to flow into the VLM call.
    Mirrors what an old client without the toggle would send.
    """
    app = create_analyze_app(sample_video, _config(language="en"))
    client = TestClient(app)
    marker_id = _mark(client)

    captured: dict[str, Any] = {}

    def fake_analyze(detection: Any, screenshot_path: Any, config: Any, **_: Any) -> UnifiedFinding:
        captured["language"] = config.language
        return _stub_finding()

    monkeypatch.setattr(
        "screenscribe.unified_analysis.analyze_finding_unified",
        fake_analyze,
    )

    response = client.post(f"/api/analyze/{marker_id}")  # no body

    assert response.status_code == 200
    assert captured["language"] == "en"


def test_analyze_endpoint_ignores_unknown_lang_and_uses_config(
    sample_video: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Stray locale strings (``"de"``, ``"xx-YY"``) fall back to config.language.

    The dashboard should never block on a bad lang hint; the toggle only
    has two valid values but a stale localStorage entry from a future
    version shouldn't take down analysis.
    """
    app = create_analyze_app(sample_video, _config(language="pl"))
    client = TestClient(app)
    marker_id = _mark(client)

    captured: dict[str, Any] = {}

    def fake_analyze(detection: Any, screenshot_path: Any, config: Any, **_: Any) -> UnifiedFinding:
        captured["language"] = config.language
        return _stub_finding()

    monkeypatch.setattr(
        "screenscribe.unified_analysis.analyze_finding_unified",
        fake_analyze,
    )

    response = client.post(f"/api/analyze/{marker_id}", json={"lang": "de"})

    assert response.status_code == 200
    assert captured["language"] == "pl"


def test_analyze_endpoint_accepts_locale_tag_and_normalises(
    sample_video: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``"pl-PL"`` resolves to ``"pl"`` (only first two chars consulted).

    Some browsers / users may pass full BCP-47 locale tags. The toggle
    itself only emits two-letter codes, but the server must be lenient.
    """
    app = create_analyze_app(sample_video, _config(language="en"))
    client = TestClient(app)
    marker_id = _mark(client)

    captured: dict[str, Any] = {}

    def fake_analyze(detection: Any, screenshot_path: Any, config: Any, **_: Any) -> UnifiedFinding:
        captured["language"] = config.language
        return _stub_finding()

    monkeypatch.setattr(
        "screenscribe.unified_analysis.analyze_finding_unified",
        fake_analyze,
    )

    response = client.post(f"/api/analyze/{marker_id}", json={"lang": "pl-PL"})

    assert response.status_code == 200
    assert captured["language"] == "pl"


def test_dashboard_exposes_export_tab(sample_video: Path) -> None:
    """A third header tab Export/Eksport plus its pane must ship (3-step flow)."""
    app = create_analyze_app(sample_video, _config())
    html = TestClient(app).get("/").text
    assert 'data-tab="export"' in html
    assert 'data-i18n="tab_export"' in html
    assert 'id="tab-export"' in html
    # Existing panes still present (no regression).
    assert 'id="tab-capture"' in html
    assert 'id="tab-findings"' in html
    # The export bar moved out of the old bottom island.
    assert 'class="sidebar-footer"' not in html


def test_dashboard_gates_export_until_first_moment(sample_video: Path) -> None:
    """Export actions ship disabled with a helper line, until a moment exists."""
    app = create_analyze_app(sample_video, _config(language="pl"))
    html = TestClient(app).get("/").text
    assert 'id="exportJsonBtn"' in html
    assert 'data-i18n="export_gate_hint"' in html
    assert "Export is available after you add your first moment." in html
    assert "Eksport będzie dostępny po dodaniu pierwszego momentu." in html
    # Both export buttons render with the native disabled attribute at zero markers.
    assert re.search(r'id="exportJsonBtn"[^>]*\sdisabled', html)
    assert re.search(r'id="finalizeBtn"[^>]*\sdisabled', html)


def test_dashboard_exposes_how_it_works_helper(sample_video: Path) -> None:
    """First-run 'How it works' helper ships with four keyed steps."""
    app = create_analyze_app(sample_video, _config())
    html = TestClient(app).get("/").text
    for n in (1, 2, 3, 4):
        assert f'data-i18n="howto_step_{n}"' in html
    # Gated on the empty-state body flag.
    assert 'data-has-markers="false"' in html


def test_dashboard_exposes_reactive_video_status(sample_video: Path) -> None:
    """Reactive status line under the player ships, keyed in both languages."""
    app = create_analyze_app(sample_video, _config())
    html = TestClient(app).get("/").text
    assert 'id="videoStatusLine"' in html
    assert 'data-i18n="video_status_idle"' in html
    assert "Pause the video to mark a moment" in html
    assert "Zatrzymaj film, żeby oznaczyć moment" in html


def test_markers_api_reflects_added_moment(sample_video: Path) -> None:
    """The export gate and empty-state flip read from /api/markers; confirm a
    marked moment actually lands there (the data the JS gate keys off). The
    client-side enable/hide toggle itself is covered by manual browser checks."""
    app = create_analyze_app(sample_video, _config())
    client = TestClient(app)
    assert client.get("/api/markers").json() == []
    _mark(client)
    markers = client.get("/api/markers").json()
    assert len(markers) == 1
