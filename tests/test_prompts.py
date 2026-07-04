import pytest

from screenscribe.prompts import (
    apply_analysis_prompt_override,
    get_unified_analysis_prompt,
)


@pytest.mark.parametrize("text_only", [False, True])
def test_unified_prompt_pl_directs_polish_output(text_only: bool) -> None:
    """A8: the PL analysis prompt must tell the model to write JSON values in
    Polish — otherwise the model defaults to English even when language='pl'
    (e.g. a PL note came back as 'User states they are ready for analysis')."""
    prompt = get_unified_analysis_prompt("pl", text_only=text_only)
    assert "PO POLSKU" in prompt


@pytest.mark.parametrize("text_only", [False, True])
def test_unified_prompt_en_directs_english_output(text_only: bool) -> None:
    prompt = get_unified_analysis_prompt("en", text_only=text_only)
    assert "in English" in prompt


def test_apply_analysis_prompt_override_keeps_base_prompt_when_blank() -> None:
    base_prompt = "Analyze this screenshot and respond in JSON."

    assert apply_analysis_prompt_override(base_prompt, "") == base_prompt
    assert apply_analysis_prompt_override(base_prompt, "   ") == base_prompt


def test_apply_analysis_prompt_override_appends_operator_instructions() -> None:
    base_prompt = "Analyze this screenshot and respond in JSON."
    override = "Focus on auth resilience and cross-device completion."

    result = apply_analysis_prompt_override(base_prompt, override)

    assert base_prompt in result
    assert override in result
    assert "Preserve every required field" in result
