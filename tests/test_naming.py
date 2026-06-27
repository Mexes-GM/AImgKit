"""Tests for unique_save_path collision avoidance and naming (uses core modules)."""
import threading
import os
from collections import defaultdict

from core.io_save import unique_save_path
from core.naming import build_output_filename


def _make_reserved():
    return set(), threading.Lock()


def test_first_call_unchanged(tmp_path):
    reserved, lock = _make_reserved()
    path = str(tmp_path / "out.png")
    result = unique_save_path(path, reserved, lock)
    assert result == path
    assert path in reserved


def test_second_call_same_path_gets_suffix(tmp_path):
    reserved, lock = _make_reserved()
    path = str(tmp_path / "out.png")
    first = unique_save_path(path, reserved, lock)
    second = unique_save_path(path, reserved, lock)
    assert first == path
    assert second == str(tmp_path / "out_1.png")


def test_disk_conflict_gets_suffix(tmp_path):
    """If a file already exists on disk, first call should still get _1."""
    reserved, lock = _make_reserved()
    existing = tmp_path / "out.png"
    existing.write_bytes(b"")
    result = unique_save_path(str(existing), reserved, lock)
    assert result == str(tmp_path / "out_1.png")


def test_multiple_conflicts_increment(tmp_path):
    reserved, lock = _make_reserved()
    path = str(tmp_path / "out.png")
    r0 = unique_save_path(path, reserved, lock)
    r1 = unique_save_path(path, reserved, lock)
    r2 = unique_save_path(path, reserved, lock)
    assert r0 == str(tmp_path / "out.png")
    assert r1 == str(tmp_path / "out_1.png")
    assert r2 == str(tmp_path / "out_2.png")


def test_video_path_equals_source_gets_unique(tmp_path):
    """FIX-05 scenario: video save_path == video_path must yield a different path."""
    reserved, lock = _make_reserved()
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"")
    result = unique_save_path(str(video), reserved, lock)
    assert result != str(video)
    assert result == str(tmp_path / "clip_1.mp4")


# ── FIX-08: sanitize_for_filename edge cases ──────────────────────────────
from comfy_metadata import sanitize_for_filename


def test_reserved_name_con():
    result = sanitize_for_filename('con')
    assert result.lower() != 'con', f"Expected reserved name to be prefixed, got {result!r}"


def test_reserved_name_empty_input():
    result = sanitize_for_filename('???')
    assert result, "Expected non-empty result for all-illegal characters"
    assert result == 'unnamed', f"Expected 'unnamed', got {result!r}"


def test_build_output_filename_length(tmp_path):
    """Stem from build_output_filename must not exceed 120 chars."""
    lock = threading.Lock()
    counters = defaultdict(int)
    image_path = str(tmp_path / "img.png")
    long_chars = [f"character_name_{i}" for i in range(30)]  # >120 chars joined
    autoname_map = {image_path: long_chars}

    filename = build_output_filename(image_path, autoname_map, counters, lock)
    stem = filename.rsplit('_', 1)[0]  # strip _N counter
    assert len(stem) <= 120, f"Stem length {len(stem)} exceeds 120 chars"
