"""Integration tests for screenscribe with real API calls.

These tests require:
- API key configured in ~/.config/screenscribe/config.env
- Or LIBRAXIS_API_KEY environment variable
- Network access to api.libraxis.cloud

Run with: make test-integration
"""

import json
import time
from pathlib import Path

import pytest

from screenscribe.config import ScreenScribeConfig
from screenscribe.detect import Detection
from screenscribe.semantic_filter import (
    PointOfInterest,
    semantic_prefilter,
)
from screenscribe.transcribe import Segment, TranscriptionResult
from screenscribe.unified_analysis import UnifiedFinding

# Integration tests (network/API) are marked per-class below so that
# self-contained tests in this module (e.g. checkpoint schema) still run by
# default. Classes that hit the real API carry @pytest.mark.integration.


@pytest.fixture
def config_with_api() -> ScreenScribeConfig:
    """Config with API key from config file or environment."""
    config = ScreenScribeConfig.load()
    if not config.get_llm_api_key():
        pytest.skip("No API key configured (set in ~/.config/screenscribe/config.env)")
    return config


@pytest.fixture
def sample_transcription_pl() -> TranscriptionResult:
    """Sample Polish transcription for testing."""
    return TranscriptionResult(
        text=(
            "Tutaj widzę problem z przyciskiem. Nie reaguje na kliknięcie. "
            "Trzeba to naprawić. Layout wygląda dobrze ale kolory są za ciemne. "
            "Formularz rejestracji ma błąd walidacji."
        ),
        segments=[
            Segment(id=0, start=0.0, end=4.0, text="Tutaj widzę problem z przyciskiem."),
            Segment(id=1, start=4.5, end=7.0, text="Nie reaguje na kliknięcie."),
            Segment(id=2, start=7.5, end=10.0, text="Trzeba to naprawić."),
            Segment(
                id=3, start=11.0, end=16.0, text="Layout wygląda dobrze ale kolory są za ciemne."
            ),
            Segment(id=4, start=17.0, end=21.0, text="Formularz rejestracji ma błąd walidacji."),
        ],
        language="pl",
    )


@pytest.fixture
def sample_transcription_en() -> TranscriptionResult:
    """Sample English transcription for testing."""
    return TranscriptionResult(
        text=(
            "I see a bug with the submit button. It doesn't respond to clicks. "
            "We need to fix this. The layout looks good but colors are too dark. "
            "The registration form has a validation error."
        ),
        segments=[
            Segment(id=0, start=0.0, end=4.0, text="I see a bug with the submit button."),
            Segment(id=1, start=4.5, end=7.0, text="It doesn't respond to clicks."),
            Segment(id=2, start=7.5, end=10.0, text="We need to fix this."),
            Segment(
                id=3, start=11.0, end=16.0, text="The layout looks good but colors are too dark."
            ),
            Segment(
                id=4, start=17.0, end=21.0, text="The registration form has a validation error."
            ),
        ],
        language="en",
    )


@pytest.fixture
def sample_detection() -> Detection:
    """Sample detection for semantic analysis."""
    return Detection(
        segment=Segment(id=0, start=0.0, end=4.0, text="Tutaj widzę problem z przyciskiem."),
        category="bug",
        keywords_found=["problem"],
        context="Tutaj widzę problem z przyciskiem. Nie reaguje na kliknięcie.",
    )


# ============================================================================
# Semantic Pre-filter Integration Tests
# ============================================================================


@pytest.mark.integration
class TestSemanticPrefilterIntegration:
    """Integration tests for semantic_prefilter with real API."""

    @pytest.mark.slow
    def test_prefilter_polish_transcription(
        self,
        config_with_api: ScreenScribeConfig,
        sample_transcription_pl: TranscriptionResult,
    ) -> None:
        """Semantic pre-filter identifies issues in Polish transcription."""
        config_with_api.language = "pl"

        result = semantic_prefilter(sample_transcription_pl, config_with_api)
        pois = result.pois

        # Should identify at least some issues
        assert len(pois) >= 1, "Should identify at least one point of interest"

        # All POIs should be valid
        for poi in pois:
            assert isinstance(poi, PointOfInterest)
            assert poi.timestamp_start >= 0
            assert poi.timestamp_end > poi.timestamp_start
            assert poi.category in ("bug", "change", "ui", "performance", "accessibility", "other")
            assert 0.0 <= poi.confidence <= 1.0
            assert poi.reasoning  # Should have reasoning

    @pytest.mark.slow
    def test_prefilter_english_transcription(
        self,
        config_with_api: ScreenScribeConfig,
        sample_transcription_en: TranscriptionResult,
    ) -> None:
        """Semantic pre-filter identifies issues in English transcription."""
        config_with_api.language = "en"

        result = semantic_prefilter(sample_transcription_en, config_with_api)
        pois = result.pois

        # Should identify at least some issues
        assert len(pois) >= 1, "Should identify at least one point of interest"

        # Check categories make sense
        categories = {poi.category for poi in pois}
        assert len(categories) >= 1

    @pytest.mark.slow
    def test_prefilter_finds_bug_category(
        self,
        config_with_api: ScreenScribeConfig,
        sample_transcription_pl: TranscriptionResult,
    ) -> None:
        """Pre-filter should identify bug category from problem description."""
        config_with_api.language = "pl"

        result = semantic_prefilter(sample_transcription_pl, config_with_api)
        pois = result.pois

        # At least one should be bug-related
        bug_pois = [p for p in pois if p.category == "bug"]
        assert len(bug_pois) >= 1, "Should identify bug from 'problem z przyciskiem'"

    @pytest.mark.slow
    def test_prefilter_returns_valid_timestamps(
        self,
        config_with_api: ScreenScribeConfig,
        sample_transcription_pl: TranscriptionResult,
    ) -> None:
        """Pre-filter timestamps should be within transcript range."""
        config_with_api.language = "pl"

        result = semantic_prefilter(sample_transcription_pl, config_with_api)
        pois = result.pois

        # Get transcript time range
        min_time = min(s.start for s in sample_transcription_pl.segments)
        max_time = max(s.end for s in sample_transcription_pl.segments)

        for poi in pois:
            # Allow some tolerance for LLM timestamp estimation
            assert poi.timestamp_start >= min_time - 1.0, (
                f"Start {poi.timestamp_start} before transcript"
            )
            assert poi.timestamp_end <= max_time + 1.0, f"End {poi.timestamp_end} after transcript"


# ============================================================================
# Checkpoint Schema Tests
# ============================================================================


class TestCheckpointSchema:
    """Checkpoint load behaviour across schema versions (no network needed)."""

    def test_load_checkpoint_skips_legacy_old_shape_gracefully(self, tmp_path: Path) -> None:
        """An old-shape checkpoint (no/low schema_version) is skipped, not raised on."""
        from screenscribe.checkpoint import (
            CHECKPOINT_SCHEMA_VERSION,
            get_checkpoint_dir,
            get_checkpoint_path,
            load_checkpoint,
        )

        # Pre-schema-bump checkpoint: carries the dropped legacy
        # `semantic_analyses` payload and lacks the schema_version marker.
        get_checkpoint_dir(tmp_path).mkdir(parents=True, exist_ok=True)
        legacy_payload = {
            "video_path": str(tmp_path / "old.mp4"),
            "video_hash": "deadbeefdeadbeef",
            "output_dir": str(tmp_path),
            "language": "en",
            "completed_stages": ["audio", "transcription"],
            "semantic_analyses": [
                {
                    "detection_id": 0,
                    "category": "bug",
                    "is_issue": True,
                    "sentiment": "problem",
                    "severity": "high",
                    "summary": "legacy",
                    "action_items": [],
                    "affected_components": [],
                    "suggested_fix": "",
                }
            ],
        }
        with open(get_checkpoint_path(tmp_path), "w", encoding="utf-8") as f:
            json.dump(legacy_payload, f)

        # Must NOT raise; must skip the stale checkpoint by returning None.
        result = load_checkpoint(tmp_path)
        assert result is None
        assert CHECKPOINT_SCHEMA_VERSION >= 2

    def test_load_checkpoint_roundtrips_current_schema(self, tmp_path: Path) -> None:
        """A checkpoint saved with the current schema loads back successfully."""
        from screenscribe.checkpoint import (
            CHECKPOINT_SCHEMA_VERSION,
            PipelineCheckpoint,
            load_checkpoint,
            save_checkpoint,
        )

        checkpoint = PipelineCheckpoint(
            video_path=str(tmp_path / "new.mp4"),
            video_hash="cafebabecafebabe",
            output_dir=str(tmp_path),
            language="en",
            schema_version=CHECKPOINT_SCHEMA_VERSION,
        )
        save_checkpoint(checkpoint, tmp_path)

        loaded = load_checkpoint(tmp_path)
        assert loaded is not None
        assert loaded.schema_version == CHECKPOINT_SCHEMA_VERSION
        assert loaded.video_hash == "cafebabecafebabe"


# ============================================================================
# Config Integration Tests
# ============================================================================


@pytest.mark.integration
class TestConfigIntegration:
    """Integration tests for configuration with API."""

    def test_config_loads_api_key(self, config_with_api: ScreenScribeConfig) -> None:
        """Config correctly loads API key from config file or environment."""
        assert config_with_api.get_llm_api_key()

    def test_config_has_valid_endpoints(self, config_with_api: ScreenScribeConfig) -> None:
        """Config has valid API endpoints."""
        # Endpoints should be HTTPS URLs with proper paths
        assert config_with_api.llm_endpoint.startswith("https://")
        assert "/v1/" in config_with_api.llm_endpoint
        assert config_with_api.stt_endpoint.startswith("https://")
        assert "/v1/" in config_with_api.stt_endpoint


# ============================================================================
# End-to-End Workflow Tests
# ============================================================================


@pytest.mark.integration
class TestEndToEndWorkflow:
    """End-to-end workflow tests."""

    @pytest.mark.slow
    def test_full_semantic_pipeline_polish(
        self,
        config_with_api: ScreenScribeConfig,
        sample_transcription_pl: TranscriptionResult,
    ) -> None:
        """Full semantic pipeline: prefilter -> convert -> analyze."""
        from screenscribe.semantic_filter import pois_to_detections

        config_with_api.language = "pl"

        # Step 1: Semantic pre-filter
        result = semantic_prefilter(sample_transcription_pl, config_with_api)
        pois = result.pois
        assert len(pois) >= 1, "Pre-filter should find issues"

        # Step 2: Convert to detections
        detections = pois_to_detections(pois, sample_transcription_pl)
        assert len(detections) == len(pois)
        assert detections, "Pre-filter should yield at least one detection"


# ============================================================================
# Analyze Server Tests
# ============================================================================


@pytest.mark.integration
class TestAnalyzeServer:
    """Integration tests for analyze server."""

    @pytest.fixture
    def sample_video(self, tmp_path: Path) -> Path:
        """Create a minimal valid video file for testing."""
        # Create a tiny valid MP4 (ftyp box only - enough for server to accept)
        video_path = tmp_path / "test_video.mp4"
        # Minimal ftyp box that makes file recognizable as MP4
        ftyp = b"\x00\x00\x00\x14ftypmp42\x00\x00\x00\x00mp42"
        video_path.write_bytes(ftyp)
        return video_path

    def test_analyze_app_creates(
        self, config_with_api: ScreenScribeConfig, sample_video: Path
    ) -> None:
        """Analyze app is created successfully."""
        from screenscribe.analyze_server import create_analyze_app

        app = create_analyze_app(sample_video, config_with_api)

        assert app is not None
        assert app.title == "Screenscribe Analyze"

    def test_analyze_index_returns_html(
        self, config_with_api: ScreenScribeConfig, sample_video: Path
    ) -> None:
        """Index endpoint returns HTML page."""
        from fastapi.testclient import TestClient

        from screenscribe.analyze_server import create_analyze_app

        app = create_analyze_app(sample_video, config_with_api)
        client = TestClient(app)

        response = client.get("/")

        assert response.status_code == 200
        assert "text/html" in response.headers["content-type"]
        assert "Screenscribe Analyze" in response.text

    def test_analyze_markers_empty_initially(
        self, config_with_api: ScreenScribeConfig, sample_video: Path
    ) -> None:
        """Markers endpoint returns empty list initially."""
        from fastapi.testclient import TestClient

        from screenscribe.analyze_server import create_analyze_app

        app = create_analyze_app(sample_video, config_with_api)
        client = TestClient(app)

        response = client.get("/api/markers")

        assert response.status_code == 200
        assert response.json() == []

    def test_analyze_mark_frame(
        self, config_with_api: ScreenScribeConfig, sample_video: Path
    ) -> None:
        """Mark frame endpoint creates marker."""
        from fastapi.testclient import TestClient

        from screenscribe.analyze_server import create_analyze_app

        app = create_analyze_app(sample_video, config_with_api)
        client = TestClient(app)

        # Mark a frame
        response = client.post(
            "/api/mark",
            json={
                "timestamp": 5.0,
                "frame_base64": "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==",
                "transcript": "Test transcript",
                "notes": "Test notes",
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert "marker_id" in data
        assert data["status"] == "pending"

        # Verify marker exists
        markers = client.get("/api/markers").json()
        assert len(markers) == 1
        assert markers[0]["timestamp"] == 5.0

    def test_analyze_page_respects_language_setting(
        self, config_with_api: ScreenScribeConfig, sample_video: Path
    ) -> None:
        """Page sets HTML lang attribute based on config."""
        from fastapi.testclient import TestClient

        from screenscribe.analyze_server import create_analyze_app

        # Test PL
        config_with_api.language = "pl"
        app = create_analyze_app(sample_video, config_with_api)
        client = TestClient(app)
        response = client.get("/")
        assert 'lang="pl"' in response.text

        # Test EN
        config_with_api.language = "en"
        app = create_analyze_app(sample_video, config_with_api)
        client = TestClient(app)
        response = client.get("/")
        assert 'lang="en"' in response.text

    def test_analyze_page_has_ui_controls(
        self, config_with_api: ScreenScribeConfig, sample_video: Path
    ) -> None:
        """Page has Mark Frame and Record controls."""
        from fastapi.testclient import TestClient

        from screenscribe.analyze_server import create_analyze_app

        app = create_analyze_app(sample_video, config_with_api)
        client = TestClient(app)

        response = client.get("/")
        html = response.text

        # UI control buttons
        assert "Mark Frame" in html or "markFrame" in html
        assert "Record" in html or "record" in html.lower()

    def test_analyze_page_has_video_player(
        self, config_with_api: ScreenScribeConfig, sample_video: Path
    ) -> None:
        """Page contains video player element."""
        from fastapi.testclient import TestClient

        from screenscribe.analyze_server import create_analyze_app

        app = create_analyze_app(sample_video, config_with_api)
        client = TestClient(app)

        response = client.get("/")
        html = response.text

        # Video player
        assert "<video" in html
        assert 'id="videoPlayer"' in html or 'id="video"' in html

    def test_analyze_page_has_mic_button(
        self, config_with_api: ScreenScribeConfig, sample_video: Path
    ) -> None:
        """Page contains microphone button for voice recording."""
        from fastapi.testclient import TestClient

        from screenscribe.analyze_server import create_analyze_app

        app = create_analyze_app(sample_video, config_with_api)
        client = TestClient(app)

        response = client.get("/")
        html = response.text

        # Mic button (Phosphor icon SVG or button)
        assert "mic" in html.lower() or "microphone" in html.lower() or "record" in html.lower()

    def test_analyze_page_has_theme_support(
        self, config_with_api: ScreenScribeConfig, sample_video: Path
    ) -> None:
        """Page has CSS variables for theming (light/dark mode support)."""
        from fastapi.testclient import TestClient

        from screenscribe.analyze_server import create_analyze_app

        app = create_analyze_app(sample_video, config_with_api)
        client = TestClient(app)

        response = client.get("/")
        html = response.text

        # CSS custom properties for theming
        assert "--bg" in html or "--background" in html or "prefers-color-scheme" in html

    def test_analyze_page_has_voicerecorder_js(
        self, config_with_api: ScreenScribeConfig, sample_video: Path
    ) -> None:
        """Page contains VoiceRecorder JavaScript class."""
        from fastapi.testclient import TestClient

        from screenscribe.analyze_server import create_analyze_app

        app = create_analyze_app(sample_video, config_with_api)
        client = TestClient(app)

        response = client.get("/")
        html = response.text

        # VoiceRecorder class and MediaRecorder API usage
        assert "VoiceRecorder" in html
        assert "MediaRecorder" in html

    def test_analyze_page_has_finalize_trigger(
        self, config_with_api: ScreenScribeConfig, sample_video: Path
    ) -> None:
        """Page wires finalize flow button and API call."""
        from fastapi.testclient import TestClient

        from screenscribe.analyze_server import create_analyze_app

        app = create_analyze_app(sample_video, config_with_api)
        client = TestClient(app)

        response = client.get("/")
        html = response.text

        assert 'id="finalizeBtn"' in html
        assert "fetch('/api/finalize/start'" in html
        assert "fetch('/api/finalize/status/' + jobId)" in html

    def test_finalize_analyzes_all_markers_and_returns_export(
        self,
        config_with_api: ScreenScribeConfig,
        sample_video: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Finalize endpoint processes pending markers and returns exported payload."""
        from fastapi.testclient import TestClient

        from screenscribe.analyze_server import create_analyze_app

        def fake_analyze_finding_unified(*args: object, **kwargs: object) -> UnifiedFinding:
            return UnifiedFinding(
                detection_id=1,
                screenshot_path=None,
                timestamp=5.0,
                category="ui",
                is_issue=True,
                sentiment="problem",
                severity="high",
                summary="Mock summary",
                action_items=["Mock action"],
                affected_components=["Capture controls"],
                suggested_fix="Mock fix",
                ui_elements=["button"],
                issues_detected=["alignment"],
                accessibility_notes=[],
                design_feedback="ok",
                technical_observations="ok",
                response_id="resp_mock_1",
            )

        monkeypatch.setattr(
            "screenscribe.unified_analysis.analyze_finding_unified", fake_analyze_finding_unified
        )

        app = create_analyze_app(sample_video, config_with_api)
        client = TestClient(app)

        frame_base64 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="

        for timestamp in (5.0, 9.0):
            response = client.post(
                "/api/mark",
                json={
                    "timestamp": timestamp,
                    "frame_base64": frame_base64,
                    "transcript": f"Marker {timestamp}",
                    "notes": "Test note",
                },
            )
            assert response.status_code == 200

        finalize_response = client.post("/api/finalize")
        assert finalize_response.status_code == 200
        payload = finalize_response.json()

        assert payload["analysis"]["processed"] == 2
        assert payload["analysis"]["completed"] == 2
        assert payload["analysis"]["errors"] == 0

        assert len(payload["markers"]) == 2
        assert all(marker["status"] == "completed" for marker in payload["markers"])

        exported = payload["export"]
        assert "video" in exported
        assert len(exported["work_items"]) == 2
        assert all("analysis" in item for item in exported["work_items"])

    def test_finalize_async_job_status_and_result(
        self,
        config_with_api: ScreenScribeConfig,
        sample_video: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Async finalize endpoints expose x/y progress and final export payload."""
        from fastapi.testclient import TestClient

        from screenscribe.analyze_server import create_analyze_app

        def fake_analyze_finding_unified(*args: object, **kwargs: object) -> UnifiedFinding:
            return UnifiedFinding(
                detection_id=1,
                screenshot_path=None,
                timestamp=2.0,
                category="ui",
                is_issue=True,
                sentiment="problem",
                severity="medium",
                summary="Async mock summary",
                action_items=["Mock action"],
                affected_components=["Marker panel"],
                suggested_fix="Mock fix",
                ui_elements=["panel"],
                issues_detected=["contrast"],
                accessibility_notes=[],
                design_feedback="ok",
                technical_observations="ok",
                response_id="resp_mock_async",
            )

        monkeypatch.setattr(
            "screenscribe.unified_analysis.analyze_finding_unified", fake_analyze_finding_unified
        )

        app = create_analyze_app(sample_video, config_with_api)
        client = TestClient(app)

        frame_base64 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
        for timestamp in (2.0, 4.0, 6.0):
            response = client.post(
                "/api/mark",
                json={
                    "timestamp": timestamp,
                    "frame_base64": frame_base64,
                    "transcript": f"Marker {timestamp}",
                    "notes": "Async note",
                },
            )
            assert response.status_code == 200

        start_response = client.post("/api/finalize/start")
        assert start_response.status_code == 200
        start_payload = start_response.json()
        assert "job_id" in start_payload
        job_id = start_payload["job_id"]

        status_payload = start_payload
        for _ in range(100):
            status_response = client.get(f"/api/finalize/status/{job_id}")
            assert status_response.status_code == 200
            status_payload = status_response.json()
            if status_payload["status"] != "running":
                break
            time.sleep(0.01)

        assert status_payload["status"] == "completed"
        assert status_payload["processed"] == 3
        assert status_payload["completed"] == 3
        assert status_payload["errors"] == 0

        result_response = client.get(f"/api/finalize/result/{job_id}")
        assert result_response.status_code == 200
        result_payload = result_response.json()
        assert result_payload["analysis"]["processed"] == 3
        assert len(result_payload["export"]["work_items"]) == 3
