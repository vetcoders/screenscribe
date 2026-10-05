"""Contract tests for analysis presets (``review --preset``).

Covers the preset registry (programming/casual/medical/veterinary/custom),
the keyword load priority with presets (flag > global > preset > built-in),
the custom-requires---keywords-file rule, category flow into POIs/detections
and the JSON report, and the bit-for-bit default guarantee: no --preset means
exactly the pre-preset keywords and categories.
"""

from __future__ import annotations

import json
import re
from io import StringIO
from pathlib import Path
from typing import Any

import pytest
import yaml
from rich.console import Console
from typer.testing import CliRunner

from screenscribe import cli, keywords
from screenscribe.cli import app
from screenscribe.keywords import CATEGORIES, KeywordsConfig, format_keywords_hint
from screenscribe.presets import (
    PRESET_CHOICES,
    PRESET_NAMES,
    PresetError,
    load_custom_preset,
    load_preset,
)
from screenscribe.report.console import print_report
from screenscribe.report.json_report import save_enhanced_json_report
from screenscribe.report.markdown_report import save_enhanced_markdown_report
from screenscribe.semantic_filter import (
    POI_CATEGORIES,
    PointOfInterest,
    _parse_prefilter_response,
    _validate_poi_category,
    pois_to_detections,
)
from screenscribe.transcribe import Segment, TranscriptionResult

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _plain(output: str) -> str:
    """Strip ANSI color codes and box borders, then collapse whitespace."""
    return " ".join(_ANSI_RE.sub("", output).replace("│", " ").split())


def _no_global_keywords(monkeypatch: Any, tmp_path: Path) -> None:
    """Point the global keywords path at a file that does not exist."""
    monkeypatch.setattr(keywords, "GLOBAL_KEYWORDS_PATH", tmp_path / "absent.yaml")


def _transcription() -> TranscriptionResult:
    return TranscriptionResult(
        text="Pacjent kaszle. Właściciel zgłasza apetyt bez zmian.",
        segments=[
            Segment(id=0, start=0.0, end=3.0, text="Pacjent kaszle."),
            Segment(id=1, start=3.5, end=6.0, text="Właściciel zgłasza apetyt bez zmian."),
        ],
        language="pl",
    )


# ---------------------------------------------------------------------------
# Preset registry
# ---------------------------------------------------------------------------


def test_preset_registry_loads_all_shipped_presets() -> None:
    for name in PRESET_NAMES:
        preset = load_preset(name)
        assert preset.name == name
        assert preset.categories, f"preset {name} has no categories"


def test_programming_preset_matches_builtin_defaults() -> None:
    """programming = the historical default: same categories, no own dictionary."""
    preset = load_preset("programming")
    assert preset.categories == CATEGORIES == POI_CATEGORIES
    assert preset.keywords is None  # built-in default_keywords.yaml stays the source
    assert preset.prompt == ""


def test_medical_and_veterinary_have_own_categories_and_dictionaries() -> None:
    expected = ("finding", "observation", "risk", "followup", "other")
    for name in ("medical", "veterinary"):
        preset = load_preset(name)
        assert preset.categories == expected
        assert preset.keywords is not None
        assert set(preset.keywords) == set(expected)
        phrases = [phrase for phrases in preset.keywords.values() for phrase in phrases]
        assert len(phrases) >= 15, f"{name} dictionary too small: {len(phrases)}"
        # Both Polish and English phrases must be present.
        assert any(any(ch in phrase for ch in "ąćęłńóśźż") for phrase in phrases), (
            f"{name} dictionary has no Polish-diacritic phrase"
        )
        assert any(phrase.isascii() and " " in phrase for phrase in phrases), (
            f"{name} dictionary has no English phrase"
        )
        assert preset.prompt, f"{name} preset has no prompt fragment"


def test_unknown_preset_raises_readable_error() -> None:
    with pytest.raises(PresetError, match="Unknown preset"):
        load_preset("astrophysics")


def test_custom_preset_categories_come_from_keywords_file(tmp_path: Path) -> None:
    custom_file = tmp_path / "mine.yaml"
    # Literal text (not yaml.safe_dump, which sorts keys) to pin file order.
    custom_file.write_text(
        'regression:\n  - "used to work"\ncopy:\n  - "literówka"\n',
        encoding="utf-8",
    )
    preset = load_custom_preset(custom_file)
    assert preset.name == "custom"
    assert preset.categories == ("regression", "copy")
    assert preset.keywords is None  # the file itself is the dictionary


def test_custom_preset_rejects_non_mapping_file(tmp_path: Path) -> None:
    bad_file = tmp_path / "bad.yaml"
    bad_file.write_text("- just\n- a\n- list\n", encoding="utf-8")
    with pytest.raises(PresetError, match="not a YAML mapping"):
        load_custom_preset(bad_file)


@pytest.mark.parametrize("category", ["../../../outside", "foo/bar", r"foo\bar", "space name"])
def test_custom_preset_rejects_unsafe_category_names(tmp_path: Path, category: str) -> None:
    custom_file = tmp_path / "unsafe.yaml"
    custom_file.write_text(yaml.safe_dump({category: ["phrase"]}), encoding="utf-8")

    with pytest.raises(PresetError, match="Unsafe category name"):
        load_custom_preset(custom_file)


def test_custom_preset_accepts_unicode_filename_safe_category(tmp_path: Path) -> None:
    custom_file = tmp_path / "safe.yaml"
    custom_file.write_text(yaml.safe_dump({"zażółć_2": ["phrase"]}), encoding="utf-8")

    assert load_custom_preset(custom_file).categories == ("zażółć_2",)


# ---------------------------------------------------------------------------
# Keyword load priority with presets
# ---------------------------------------------------------------------------


def test_default_load_is_bit_for_bit_identical_without_preset(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """No preset (or the programming preset) == today's behavior, exactly."""
    _no_global_keywords(monkeypatch, tmp_path)

    legacy = KeywordsConfig.load()
    via_preset = KeywordsConfig.load(preset=load_preset("programming"))

    for category in CATEGORIES:
        assert legacy.get_keywords(category) == via_preset.get_keywords(category)
    assert legacy.active_categories == via_preset.active_categories == CATEGORIES
    assert format_keywords_hint(legacy) == format_keywords_hint(via_preset)

    # And both equal the raw built-in default file.
    raw = yaml.safe_load(keywords.DEFAULT_KEYWORDS_PATH.read_text(encoding="utf-8"))
    for category in CATEGORIES:
        assert legacy.get_keywords(category) == [str(p) for p in raw.get(category, [])]


def test_preset_dictionary_is_used_when_no_flag_or_global(tmp_path: Path, monkeypatch: Any) -> None:
    _no_global_keywords(monkeypatch, tmp_path)

    config = KeywordsConfig.load(preset=load_preset("veterinary"))

    assert config.active_categories == ("finding", "observation", "risk", "followup", "other")
    assert config.get_keywords("finding"), "preset finding phrases missing"
    assert config.get_keywords("followup"), "preset followup phrases missing"
    # Non-default categories live in `extra`; fixed fields stay empty.
    assert config.bug == []
    assert config.total_keywords >= 15
    hint = format_keywords_hint(config)
    assert "- finding:" in hint and "- followup:" in hint


def test_explicit_keywords_file_beats_preset_dictionary(tmp_path: Path, monkeypatch: Any) -> None:
    _no_global_keywords(monkeypatch, tmp_path)
    explicit = tmp_path / "explicit.yaml"
    explicit.write_text(yaml.safe_dump({"finding": ["only-this-phrase"]}), encoding="utf-8")

    config = KeywordsConfig.load(explicit, preset=load_preset("medical"))

    assert config.get_keywords("finding") == ["only-this-phrase"]
    assert config.active_categories == load_preset("medical").categories


def test_global_file_beats_preset_dictionary(tmp_path: Path, monkeypatch: Any) -> None:
    global_file = tmp_path / "keywords.yaml"
    global_file.write_text(yaml.safe_dump({"bug": ["global-bug"]}), encoding="utf-8")
    monkeypatch.setattr(keywords, "GLOBAL_KEYWORDS_PATH", global_file)

    config = KeywordsConfig.load(preset=load_preset("medical"))

    assert config.get_keywords("bug") == ["global-bug"]
    # Preset categories are still the active vocabulary, just empty.
    assert config.get_keywords("finding") == []


# ---------------------------------------------------------------------------
# Category flow: prefilter parse -> detections -> JSON report -> HTML
# ---------------------------------------------------------------------------


def test_validate_poi_category_honors_active_vocabulary() -> None:
    vet = load_preset("veterinary").categories
    assert _validate_poi_category("finding", vet) == "finding"
    assert _validate_poi_category("bug", vet) == "other"  # outside the preset
    # Default vocabulary unchanged.
    assert _validate_poi_category("bug") == "bug"
    assert _validate_poi_category("finding") == "other"
    # A custom vocabulary without "other" rejects arbitrary model labels.
    with pytest.raises(ValueError, match="outside the active preset vocabulary"):
        _validate_poi_category("unexpected", ("regression", "copy"))


def test_parse_custom_preset_rejects_out_of_vocabulary_model_category() -> None:
    leaked_category = "raw-provider-category"
    content = json.dumps(
        {
            "points_of_interest": [
                {
                    "timestamp_start": 0.0,
                    "timestamp_end": 3.0,
                    "category": leaked_category,
                    "confidence": 0.9,
                    "reasoning": "provider detail",
                    "transcript_excerpt": "user narration",
                }
            ]
        }
    )

    with pytest.raises(ValueError) as error:
        _parse_prefilter_response(
            content,
            _transcription(),
            strict=True,
            categories=("regression", "copy"),
        )

    assert str(error.value) == "Model returned a category outside the active preset vocabulary"
    assert leaked_category not in str(error.value)


def test_parse_prefilter_response_keeps_preset_categories() -> None:
    content = json.dumps(
        {
            "points_of_interest": [
                {
                    "timestamp_start": 0.0,
                    "timestamp_end": 3.0,
                    "category": "finding",
                    "confidence": 0.9,
                    "reasoning": "Wet mówi o objawie",
                    "transcript_excerpt": "Pacjent kaszle.",
                },
                {
                    "timestamp_start": 3.5,
                    "timestamp_end": 6.0,
                    "category": "bug",  # not in the veterinary vocabulary
                    "confidence": 0.5,
                    "reasoning": "poza słownikiem presetu",
                    "transcript_excerpt": "coś",
                },
            ]
        }
    )
    pois = _parse_prefilter_response(
        content, _transcription(), categories=load_preset("veterinary").categories
    )
    assert [poi.category for poi in pois] == ["finding", "other"]


def test_pois_to_detections_preserves_preset_categories() -> None:
    poi = PointOfInterest(
        timestamp_start=0.0,
        timestamp_end=3.0,
        category="followup",
        confidence=0.8,
        reasoning="Zlecono kontrolę",
        transcript_excerpt="Kontrola za tydzień.",
        segment_ids=[0],
    )
    vet = load_preset("veterinary").categories
    (detection,) = pois_to_detections([poi], _transcription(), categories=vet)
    assert detection.category == "followup"
    # Default vocabulary narrows the same POI to "other".
    (default_detection,) = pois_to_detections([poi], _transcription())
    assert default_detection.category == "other"


def test_json_report_records_preset_only_for_non_default(tmp_path: Path) -> None:
    from screenscribe.detect import Detection

    detection = Detection(
        segment=Segment(id=0, start=0.0, end=3.0, text="Pacjent kaszle."),
        category="finding",
        keywords_found=["semantic:finding"],
        context="",
    )
    screenshots = [(detection, tmp_path / "shot.png")]

    default_out = tmp_path / "default_report.json"
    save_enhanced_json_report([detection], screenshots, Path("video.mov"), default_out)
    default_report = json.loads(default_out.read_text(encoding="utf-8"))
    assert "preset" not in default_report  # historical shape unchanged
    assert set(default_report["summary"]) == {"total", "bugs", "changes", "ui"}

    vet = load_preset("veterinary")
    preset_out = tmp_path / "vet_report.json"
    save_enhanced_json_report(
        [detection],
        screenshots,
        Path("video.mov"),
        preset_out,
        preset_meta={"name": vet.name, "categories": list(vet.categories)},
    )
    vet_report = json.loads(preset_out.read_text(encoding="utf-8"))
    assert vet_report["preset"]["name"] == "veterinary"
    assert vet_report["preset"]["categories"] == list(vet.categories)
    assert vet_report["findings"][0]["category"] == "finding"
    assert vet_report["summary"]["categories"] == {
        "finding": 1,
        "observation": 0,
        "risk": 0,
        "followup": 0,
        "other": 0,
    }


def test_preset_markdown_and_console_show_declared_category_counts(
    tmp_path: Path, monkeypatch: Any
) -> None:
    from screenscribe.detect import Detection

    detection = Detection(
        segment=Segment(id=0, start=0.0, end=3.0, text="Pacjent kaszle."),
        category="finding",
        keywords_found=["semantic:finding"],
        context="",
    )
    screenshot = tmp_path / "shot.png"
    preset_meta = {
        "name": "veterinary",
        "categories": ["finding", "observation", "risk", "followup", "other"],
    }
    markdown_out = tmp_path / "vet_report.md"

    save_enhanced_markdown_report(
        [detection],
        [(detection, screenshot)],
        Path("video.mov"),
        markdown_out,
        transcript="Tekst widoczny na ekranie.",
        preset_meta=preset_meta,
        transcript_source="ocr",
    )
    markdown = markdown_out.read_text(encoding="utf-8")
    assert "1 finding, 0 observation, 0 risk, 0 followup, 0 other" in markdown
    assert "**Source:** OCR screen text" in markdown
    assert "audio narration" not in markdown

    sink = StringIO()
    monkeypatch.setitem(
        print_report.__globals__,
        "console",
        Console(file=sink, force_terminal=False, width=120),
    )
    print_report(
        [detection],
        [(detection, screenshot)],
        Path("video.mov"),
        categories=tuple(preset_meta["categories"]),
    )
    rendered = sink.getvalue()
    assert "finding" in rendered and "1" in rendered
    assert "risk" in rendered and "0" in rendered
    assert "Bugs" not in rendered

    sink.seek(0)
    sink.truncate(0)
    print_report([detection], [(detection, screenshot)], Path("video.mov"))
    legacy_rendered = sink.getvalue()
    assert "Bugs" in legacy_rendered
    assert "Change Requests" in legacy_rendered
    assert "UI Issues" in legacy_rendered


@pytest.mark.parametrize(
    ("source", "label"),
    [("audio", "audio narration"), ("ocr", "OCR screen text"), ("subtitle", "unknown")],
)
def test_markdown_labels_transcript_source(tmp_path: Path, source: str, label: str) -> None:
    output = tmp_path / f"{source}.md"

    save_enhanced_markdown_report(
        [],
        [],
        Path("video.mov"),
        output,
        transcript="Source text",
        transcript_source=source,
    )

    markdown = output.read_text(encoding="utf-8")
    assert f"**Source:** {label}" in markdown


def test_markdown_omits_source_label_for_legacy_call(tmp_path: Path) -> None:
    output = tmp_path / "legacy.md"

    save_enhanced_markdown_report([], [], Path("video.mov"), output, transcript="Source text")

    assert "**Source:**" not in output.read_text(encoding="utf-8")


def test_html_report_renders_preset_category_badge() -> None:
    """Unknown-to-i18n preset categories fall back to the raw upper-cased name."""
    from screenscribe.html_pro import render_html_report_pro

    html_doc = render_html_report_pro(
        video_name="consult.mov",
        video_path=None,
        generated_at="2026-09-15T10:00:00",
        executive_summary="",
        findings=[
            {
                "id": 1,
                "category": "observation",
                "timestamp": 0.0,
                "timestamp_formatted": "00:00",
                "text": "Właściciel zgłasza apetyt bez zmian.",
                "context": "",
                "unified_analysis": {"severity": "low", "summary": "Notatka"},
            }
        ],
        segments=[],
        errors=[],
        language="en",
    )
    assert "OBSERVATION" in html_doc


# ---------------------------------------------------------------------------
# CLI surface
# ---------------------------------------------------------------------------


def test_review_help_lists_preset_and_values() -> None:
    result = CliRunner().invoke(app, ["review", "--help"])
    assert result.exit_code == 0
    assert "--preset" in result.output
    for choice in PRESET_CHOICES:
        assert choice in result.output


def test_custom_preset_without_keywords_file_fails_with_instructions(
    tmp_path: Path, monkeypatch: Any
) -> None:
    monkeypatch.setattr(cli, "_check_ffmpeg_or_exit", lambda: None)
    video = tmp_path / "clip.mov"
    video.write_bytes(b"not-a-real-video")

    result = CliRunner().invoke(
        app, ["review", str(video), "--preset", "custom", "--estimate", "--no-serve"]
    )

    assert result.exit_code == 1
    output = _plain(result.output)
    assert "--preset custom requires" in output
    assert "--keywords-file" in output


def test_custom_preset_with_keywords_file_reaches_pipeline(
    tmp_path: Path, monkeypatch: Any
) -> None:
    monkeypatch.setattr(cli, "_check_ffmpeg_or_exit", lambda: None)
    video = tmp_path / "clip.mov"
    video.write_bytes(b"not-a-real-video")
    custom_file = tmp_path / "mine.yaml"
    custom_file.write_text(
        'regression:\n  - "used to work"\ncopy:\n  - "typo"\n',
        encoding="utf-8",
    )

    captured: dict[str, Any] = {}

    def _fake_run_review(videos: Any, config: Any, **kwargs: Any) -> None:
        captured["preset"] = kwargs.get("preset")
        captured["keywords"] = kwargs.get("keywords")

    monkeypatch.setattr("screenscribe.review_pipeline.run_review", _fake_run_review)

    result = CliRunner().invoke(
        app,
        [
            "review",
            str(video),
            "--preset",
            "custom",
            "--keywords-file",
            str(custom_file),
            "--estimate",
            "--no-serve",
        ],
    )

    assert result.exit_code == 0, _plain(result.output)
    preset = captured["preset"]
    assert preset.name == "custom"
    assert preset.categories == ("regression", "copy")
    loaded: KeywordsConfig = captured["keywords"]
    assert loaded.get_keywords("regression") == ["used to work"]
    assert loaded.active_categories == ("regression", "copy")


def test_unknown_preset_fails_before_pipeline(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setattr(cli, "_check_ffmpeg_or_exit", lambda: None)
    video = tmp_path / "clip.mov"
    video.write_bytes(b"not-a-real-video")

    result = CliRunner().invoke(
        app, ["review", str(video), "--preset", "nope", "--estimate", "--no-serve"]
    )

    assert result.exit_code == 1
    assert "Unknown preset" in _plain(result.output)


def test_preset_and_no_audio_reach_run_review_together(tmp_path: Path, monkeypatch: Any) -> None:
    """--preset veterinary --no-audio: CLI validation passes (mocked OCR/LLM)
    and run_review receives BOTH the OCR transcript source and the preset."""
    from screenscribe import review_pipeline
    from screenscribe.config import ScreenScribeConfig
    from screenscribe.semantic_filter import SemanticFilterResult

    monkeypatch.setattr(cli, "_check_ffmpeg_or_exit", lambda: None)
    monkeypatch.setattr("screenscribe.cli.get_video_duration", lambda _p: 12.0)
    monkeypatch.setattr("screenscribe.cli.validate_models", lambda *a, **kw: None)
    monkeypatch.setattr(
        ScreenScribeConfig,
        "load",
        classmethod(lambda cls: ScreenScribeConfig(api_key="test-key")),  # pragma: allowlist secret
    )
    monkeypatch.setattr("screenscribe.cli.has_audio_stream", lambda _p: False)

    def fake_ocr(video_path: Path, config: Any, *, frame_interval: float = 5.0) -> Any:
        return TranscriptionResult(
            text="Pacjent kaszle.",
            segments=[Segment(id=0, start=0.0, end=5.0, text="Pacjent kaszle.")],
            language="pl",
        )

    monkeypatch.setattr("screenscribe.cli.transcribe_video_ocr", fake_ocr)
    monkeypatch.setattr(
        "screenscribe.cli.semantic_prefilter",
        lambda *a, **kw: SemanticFilterResult(pois=[]),
    )

    captured: dict[str, Any] = {}
    real_run_review = review_pipeline.run_review

    def spy_run_review(videos: Any, config: Any, **kwargs: Any) -> None:
        captured["config"] = config
        captured.update(kwargs)
        real_run_review(videos, config, **kwargs)

    monkeypatch.setattr(review_pipeline, "run_review", spy_run_review)

    video = tmp_path / "consult.mov"
    video.write_bytes(b"video")
    output_dir = tmp_path / "consult_review"

    result = CliRunner().invoke(
        app,
        [
            "review",
            str(video),
            "-o",
            str(output_dir),
            "--no-serve",
            "--preset",
            "veterinary",
            "--no-audio",
        ],
    )

    assert result.exit_code == 0, _plain(result.output)
    assert captured["transcript_source"] == "ocr"
    assert captured["frame_interval"] == 5.0
    preset = captured["preset"]
    assert preset is not None
    assert preset.name == "veterinary"
    assert captured["config"].keywords is captured["keywords"]
    # The OCR-sourced run records the non-default preset in the JSON report.
    report = json.loads((output_dir / "consult_report.json").read_text(encoding="utf-8"))
    assert report["preset"]["name"] == "veterinary"
    assert report["preset"]["categories"] == list(preset.categories)
