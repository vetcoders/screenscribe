"""Transcript-only executive summary fallback using LibraxisAI LLM models."""

import httpx
from rich.console import Console

from .api_utils import (
    build_llm_request_body,
    extract_llm_response_text,
    retry_request,
)
from .config import ScreenScribeConfig
from .detect import Detection, format_timestamp
from .prompts import get_executive_summary_prompt

console = Console()


def generate_detection_executive_summary(
    detections: list[Detection], config: ScreenScribeConfig
) -> str:
    """Generate executive summary directly from transcript detections.

    This is a transcript-only fallback for runs where screenshot-backed VLM
    analysis fails, but we still want the report to surface a real AI summary.
    """
    if not detections or not config.get_llm_api_key():
        return ""

    findings_list = []
    for detection in detections:
        findings_list.append(
            f"- [{detection.category.upper()} @ {format_timestamp(detection.segment.start)}] "
            f"{detection.segment.text}"
        )

    prompt_template = get_executive_summary_prompt(config.language)
    prompt = prompt_template.format(findings=chr(10).join(findings_list))

    try:

        def do_summary_request() -> httpx.Response:
            with httpx.Client(timeout=60.0) as client:
                response = client.post(
                    config.llm_endpoint,
                    headers={
                        "Authorization": f"Bearer {config.get_llm_api_key()}",
                        "Content-Type": "application/json",
                    },
                    json=build_llm_request_body(config.llm_model, prompt, config.llm_endpoint),
                )
                response.raise_for_status()
                return response

        response = retry_request(
            do_summary_request,
            max_retries=3,
            operation_name="Transcript-only executive summary",
        )

        result = response.json()
        return extract_llm_response_text(result, config.llm_endpoint)

    except Exception as e:
        console.print(f"[yellow]Transcript-only executive summary failed: {e}[/]")
        return ""
