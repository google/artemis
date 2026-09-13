"""Tests for the unified structured-output parsing pipeline."""

from pydantic import BaseModel

from artemis.llm.structured import (
    ParseFailure,
    content_to_text,
    extract_json_candidate,
    parse_structured,
    repair_json,
)


def test_parses_fenced_json_block():
    text = 'Here is my plan.\n```json\n{"name": "click", "args": {"x": 1}}\n```\nDone.'
    result = parse_structured(text)
    assert result == {"name": "click", "args": {"x": 1}}


def test_parses_bare_json_embedded_in_prose():
    text = 'Sure! {"is_present": true, "confidence": 0.9} — hope that helps.'
    result = parse_structured(text)
    assert result == {"is_present": True, "confidence": 0.9}


def test_balanced_extraction_ignores_braces_inside_strings():
    text = '{"msg": "curly } inside", "n": [1, 2]}'
    assert extract_json_candidate(text) == text


def test_repairs_trailing_commas_and_comments():
    noisy = '{\n  // a comment\n  "a": 1, /* block */\n  "b": [1, 2,],\n}'
    assert parse_structured(noisy) == {"a": 1, "b": [1, 2]}
    # Repair must not touch string contents.
    assert repair_json('{"s": "a, } // not a comment"}') == '{"s": "a, } // not a comment"}'


def test_strips_thinking_tags_before_extraction():
    text = "<thinking>{not json}</thinking>\n```json\n[1, 2, 3]\n```"
    assert parse_structured(text) == [1, 2, 3]


def test_failure_is_typed_never_raw_text():
    result = parse_structured("no structured payload here at all")
    assert isinstance(result, ParseFailure)
    assert "no JSON" in result.error

    garbage = parse_structured("```json\n{definitely: not: json}\n```")
    assert isinstance(garbage, ParseFailure)


def test_schema_validation():
    class Verdict(BaseModel):
        is_present: bool
        confidence: float

    ok = parse_structured('{"is_present": false, "confidence": 0.4}', schema=Verdict)
    assert isinstance(ok, Verdict) and ok.confidence == 0.4

    bad = parse_structured('{"is_present": "definitely"}', schema=Verdict)
    assert isinstance(bad, ParseFailure)
    assert "schema validation failed" in bad.error


def test_content_to_text_flattens_blocks_and_skips_thinking():
    blocks = [
        {"type": "thinking", "thinking": "hidden"},
        {"type": "text", "text": "hello "},
        "raw",
        {"type": "text", "text": "world"},
    ]
    assert content_to_text(blocks) == "hello rawworld"
    assert content_to_text("plain") == "plain"
