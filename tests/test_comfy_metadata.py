import pytest

from comfy_metadata import (
    _split_tags,
    extract_character_candidates,
    sanitize_for_filename,
)


# --- sanitize_for_filename ---

def test_sanitize_removes_illegal_chars():
    assert sanitize_for_filename('a\\b/c:d*e?f"g<h>i|j') == "abcdefghij"

def test_sanitize_spaces_to_underscores():
    assert sanitize_for_filename("hello world") == "hello_world"

def test_sanitize_escaped_parens():
    assert sanitize_for_filename(r"name \(series\)") == "name_(series)"


# --- _split_tags ---

def test_split_tags_basic():
    result = _split_tags("a, b, c")
    assert result == ["a", "b", "c"]

def test_split_tags_strips_whitespace():
    result = _split_tags("  foo ,  bar  ")
    assert result == ["foo", "bar"]


# --- extract_character_candidates ---

def test_extract_character_candidates_with_pivot():
    prompt = r"hatsune miku \(vocaloid\), 1girl, blue hair, twintails"
    candidates = extract_character_candidates(prompt)
    assert any("hatsune miku" in c.lower() for c in candidates)

def test_extract_character_candidates_no_pivot_returns_something():
    prompt = "short hair, smile, looking at viewer, school uniform"
    candidates = extract_character_candidates(prompt)
    assert isinstance(candidates, list)
