"""Surface configuration for shell-composed HTML screens."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field


@dataclass(frozen=True)
class TabConfig:
    """One tab rendered by the shared tabbar partial."""

    id: str
    label_key: str
    count_id: str | None = None
    count_value_key: str | None = None


@dataclass(frozen=True)
class HeaderCellConfig:
    """One cell rendered by the shared header-right partial."""

    kind: str
    label_key: str | None = None
    value_key: str | None = None
    show_label: bool = True


@dataclass(frozen=True)
class SurfaceConfig:
    """Minimal declarative shape for server-composed screenscribe surfaces."""

    id: str
    wordmark: str
    tabs: list[TabConfig] = field(default_factory=list)
    tabs_aria_key: str = "tabs_aria"
    header_right: list[HeaderCellConfig] = field(default_factory=list)
    main_panels: list[str] = field(default_factory=list)
    sidebar_panels: list[str] = field(default_factory=list)
    footer: str | None = None
    document_footer: bool = True
    window_actions: str | None = "window_actions"
    modals: list[str] = field(default_factory=list)
    features: Mapping[str, bool] = field(default_factory=dict)
    extra_styles: list[str] = field(default_factory=list)
    scripts: list[str] = field(default_factory=list)
    i18n_namespace: str = "review"
    lang_persist_mode: str = "report"
    title_prefix: str = "Screenscribe Report"
    transcript_heading_key: str = "transcript"
    transcript_search_key: str = "searchTranscript"
    transcript_empty_state: bool = False


REVIEW_SURFACE = SurfaceConfig(
    id="review",
    wordmark="screenscribe review",
    # Browser-tab title stays brand-only: it must not reintroduce the stale
    # "Report" wording, and the localized word "Review" cannot live in an
    # untranslated <title> (it would leak English chrome into the PL render —
    # see tests/test_i18n_no_hardcoded_dom). The visible header uses the wordmark.
    title_prefix="Screenscribe",
    tabs=[
        TabConfig("summary", "summary"),
        TabConfig("findings", "findings", count_value_key="findings_count"),
        TabConfig("export", "export"),
    ],
    tabs_aria_key="tabs_aria",
    header_right=[
        HeaderCellConfig("meta", value_key="video_name_escaped", show_label=False),
        HeaderCellConfig("timestamp", value_key="display_time_escaped", show_label=False),
        HeaderCellConfig("lang_toggle", show_label=False),
    ],
    main_panels=["video_panel", "transcript_panel"],
    sidebar_panels=["review_sidebar"],
    footer="sidebar_footer",
    modals=["lightbox", "manual_frame_modal"],
    features={
        "workspace": True,
        "review": True,
        "manual_frames": True,
        "exports": True,
    },
    scripts=[
        "i18n",
        "lib/language-control",
        "lib/stt-transport",
        "lib/tab-keyboard",
        "video_player",
        "review_app",
    ],
    i18n_namespace="review",
    lang_persist_mode="report",
)


ANALYZE_SURFACE = SurfaceConfig(
    id="analyze",
    wordmark="analyze",
    tabs=[
        TabConfig("capture", "tab_capture"),
        TabConfig(
            "findings", "tab_findings", count_id="findings-count", count_value_key="findings_count"
        ),
        TabConfig("export", "tab_export"),
    ],
    tabs_aria_key="tabs_aria",
    header_right=[
        HeaderCellConfig("meta", value_key="video_name_escaped", show_label=False),
        HeaderCellConfig("mode", label_key="meta_mode", show_label=False),
        HeaderCellConfig("speech_lang", label_key="speech_language_label"),
        HeaderCellConfig("lang_toggle", label_key="ui_language_label"),
    ],
    main_panels=["video_panel", "transcript_panel"],
    sidebar_panels=["capture_panel", "voice_notes_panel", "export_panel"],
    footer=None,
    document_footer=False,
    window_actions=None,
    modals=["frame_modal"],
    features={
        "marker_timeline": True,
        "native_video": True,
    },
    extra_styles=["analyze_dashboard"],
    scripts=[
        "i18n",
        "lib/language-control",
        "lib/stt-transport",
        "lib/tab-keyboard",
        "analyze_dashboard",
    ],
    i18n_namespace="analyze",
    lang_persist_mode="string",
    title_prefix="Screenscribe Analyze",
    transcript_heading_key="transcript_heading",
    transcript_search_key="transcript_search",
    transcript_empty_state=True,
)
