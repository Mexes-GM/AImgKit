"""Tests for core/ modules (REF-01 through REF-04)."""
from __future__ import annotations
import os
import threading
from collections import defaultdict

import pytest

from core.watermark import compute_watermark_width, position_for_corner, WATERMARK_MIN_RELATIVE, WATERMARK_MAX_RELATIVE
from core.corner import CornerSelector, CORNERS
from core.io_save import unique_save_path
from core.naming import build_output_filename


# ── compute_watermark_width ───────────────────────────────────────────────

def test_compute_watermark_width_normal():
    result = compute_watermark_width(1000, 28.4)
    assert isinstance(result, int)
    assert result == 284


def test_compute_watermark_width_clamp_min():
    # size_pct=1 → target=10, min=60; should clamp to min
    result = compute_watermark_width(1000, 1.0)
    assert result == int(1000 * WATERMARK_MIN_RELATIVE)


def test_compute_watermark_width_clamp_max():
    # size_pct=99 → target=990, max=500; should clamp to max
    result = compute_watermark_width(1000, 99.0)
    assert result == int(1000 * WATERMARK_MAX_RELATIVE)


# ── position_for_corner ───────────────────────────────────────────────────

@pytest.mark.parametrize("corner,expected", [
    ("bottom-left",  (10, 90)),
    ("bottom-right", (90, 90)),
    ("top-left",     (10, 10)),
    ("top-right",    (90, 10)),
])
def test_position_for_corner_all(corner, expected):
    # 200×200 base, 100×100 wm, margin=10
    result = position_for_corner(200, 200, 100, 100, corner, margin=10)
    assert result == expected


def test_position_for_corner_default_is_bottom_left():
    result = position_for_corner(200, 200, 100, 100, "unknown", margin=10)
    assert result == (10, 90)


def test_position_for_corner_clamps_to_zero():
    # wm larger than base → negative raw position → clamp to 0
    result = position_for_corner(50, 50, 200, 200, "bottom-left", margin=10)
    assert result[0] >= 0
    assert result[1] >= 0


# ── CornerSelector ────────────────────────────────────────────────────────

def test_corner_selector_no_consecutive_repeats():
    sel = CornerSelector()
    prev = sel.choose()
    for _ in range(19):
        cur = sel.choose()
        assert cur != prev, f"Consecutive repeat: {prev!r} → {cur!r}"
        prev = cur


def test_corner_selector_reset_allows_any():
    sel = CornerSelector()
    sel.choose()
    sel.reset()
    # After reset _last is None, so all 4 corners are possible
    results = {sel.choose() for _ in range(40)}
    assert len(results) > 1  # sanity: not stuck on one corner


def test_corner_selector_values_in_corners():
    sel = CornerSelector()
    for _ in range(20):
        assert sel.choose() in CORNERS


# ── unique_save_path ──────────────────────────────────────────────────────

def test_unique_save_path_no_conflict(tmp_path):
    lock = threading.Lock()
    reserved: set[str] = set()
    path = str(tmp_path / "out.png")
    result = unique_save_path(path, reserved, lock)
    assert result == path
    assert path in reserved


def test_unique_save_path_reserved_conflict(tmp_path):
    lock = threading.Lock()
    reserved: set[str] = set()
    path = str(tmp_path / "out.png")
    first = unique_save_path(path, reserved, lock)
    second = unique_save_path(path, reserved, lock)
    assert first == path
    assert second == str(tmp_path / "out_1.png")


def test_unique_save_path_disk_conflict(tmp_path):
    lock = threading.Lock()
    reserved: set[str] = set()
    existing = tmp_path / "out.png"
    existing.write_bytes(b"")
    result = unique_save_path(str(existing), reserved, lock)
    assert result == str(tmp_path / "out_1.png")


# ── build_output_filename ─────────────────────────────────────────────────

def test_build_output_filename_no_autoname(tmp_path):
    lock = threading.Lock()
    counters: defaultdict = defaultdict(int)
    path = str(tmp_path / "image.png")
    result = build_output_filename(path, {}, counters, lock)
    assert result == "image.png"


def test_build_output_filename_with_chars(tmp_path):
    lock = threading.Lock()
    counters: defaultdict = defaultdict(int)
    path = str(tmp_path / "image.png")
    autoname_map = {path: ["Alice", "Bob"]}
    result = build_output_filename(path, autoname_map, counters, lock)
    assert result == "Alice+Bob_1.png"


def test_build_output_filename_counter_increments(tmp_path):
    lock = threading.Lock()
    counters: defaultdict = defaultdict(int)
    path1 = str(tmp_path / "a.png")
    path2 = str(tmp_path / "b.png")
    autoname_map = {path1: ["Alice"], path2: ["Alice"]}
    r1 = build_output_filename(path1, autoname_map, counters, lock)
    r2 = build_output_filename(path2, autoname_map, counters, lock)
    assert r1 == "Alice_1.png"
    assert r2 == "Alice_2.png"


def test_build_output_filename_stem_max_len(tmp_path):
    lock = threading.Lock()
    counters: defaultdict = defaultdict(int)
    path = str(tmp_path / "img.png")
    long_chars = [f"character_name_{i}" for i in range(30)]
    autoname_map = {path: long_chars}
    result = build_output_filename(path, autoname_map, counters, lock)
    stem = result.rsplit("_", 1)[0]
    assert len(stem) <= 120
