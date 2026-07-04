"""Demo surface proving shell composition is config-only."""

from __future__ import annotations

from .surface import HeaderCellConfig, SurfaceConfig, TabConfig

DEMO_SURFACE = SurfaceConfig(
    id="viewer",
    wordmark="Screenscribe Viewer",
    tabs=[
        TabConfig("capture", "tab_capture"),
        TabConfig(
            "findings", "tab_findings", count_id="findings-count", count_value_key="findings_count"
        ),
    ],
    header_right=[
        HeaderCellConfig("meta", value_key="video_name_escaped", show_label=False),
        HeaderCellConfig("lang_toggle", label_key="ui_language_label"),
    ],
    main_panels=["video_panel", "transcript_panel"],
    footer=None,
    document_footer=False,
    window_actions=None,
    modals=[],
    features={
        "native_video": True,
        "readonly": True,
    },
    i18n_namespace="viewer",
    lang_persist_mode="string",
    title_prefix="Screenscribe Viewer",
    transcript_heading_key="transcript_heading",
    transcript_search_key="transcript_search",
    transcript_empty_state=True,
)


__all__ = ["DEMO_SURFACE"]
