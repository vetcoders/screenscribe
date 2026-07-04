"""Unit tests for parse_json_response sentinel behavior."""

from screenscribe.unified_analysis import parse_json_response


def test_parse_json_response_malformed() -> None:
    """Malformed JSON returns a sentinel dict (no raise) with parse_error + raw_content."""
    bad = "this is not json at all {definitely[ broken"
    result = parse_json_response(bad)

    assert isinstance(result, dict)
    assert "parse_error" in result
    assert "raw_content" in result
    assert result["raw_content"] == bad
    # The sentinel is the failure path, not a real parse.
    assert "summary" not in result


def test_parse_json_response_valid() -> None:
    """Well-formed JSON parses normally without the sentinel keys."""
    result = parse_json_response('{"summary": "ok", "action_items": []}')

    assert result == {"summary": "ok", "action_items": []}
    assert "parse_error" not in result
