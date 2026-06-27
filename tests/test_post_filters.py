import numpy as np
from PIL import Image
import pytest

from post_filters import (
    apply_pipeline,
    gaussian_noise,
    kuwahara_blur,
    median_filter_image,
    resize_relative,
)


def _img(mode="RGB", size=(64, 64)):
    arr = np.random.randint(0, 256, (*size[::-1], {"RGB": 3, "RGBA": 4, "L": None}[mode] or 1), dtype=np.uint8)
    if mode == "L":
        arr = arr[..., 0]
    return Image.fromarray(arr, mode=mode)


# --- resize_relative ---

def test_resize_scale_1_returns_same():
    img = _img()
    out = resize_relative(img, 1.0, 1.0)
    assert out is img

def test_resize_scale_2_doubles_dimensions():
    img = _img(size=(64, 40))
    out = resize_relative(img, 2.0, 2.0)
    assert out.size == (128, 80)


# --- kuwahara_blur ---

def test_kuwahara_radius_0_returns_same():
    img = _img()
    out = kuwahara_blur(img, radius=0)
    assert out is img

def test_kuwahara_radius_positive_rgb():
    img = _img("RGB")
    out = kuwahara_blur(img, radius=2)
    assert out.size == img.size and out.mode == "RGB"

def test_kuwahara_radius_positive_rgba():
    img = _img("RGBA")
    out = kuwahara_blur(img, radius=2)
    assert out.size == img.size and out.mode == "RGBA"

def test_kuwahara_radius_positive_l():
    img = _img("L")
    out = kuwahara_blur(img, radius=2)
    assert out.size == img.size and out.mode == "L"


# --- median_filter_image ---

def test_median_size_0_is_noop():
    img = _img()
    out = median_filter_image(img, size=0)
    assert out is img

def test_median_size_positive_works():
    img = _img()
    out = median_filter_image(img, size=1)
    assert out.size == img.size


# --- gaussian_noise ---

def test_gaussian_noise_strength_0_is_noop():
    img = _img()
    out = gaussian_noise(img, strength=0)
    assert out is img

def test_gaussian_noise_seed_reproducible():
    img = _img()
    out1 = gaussian_noise(img, strength=0.5, seed=42)
    out2 = gaussian_noise(img, strength=0.5, seed=42)
    assert np.array_equal(np.asarray(out1), np.asarray(out2))


# --- apply_pipeline ---

def test_apply_pipeline_disabled_is_noop():
    img = _img()
    out = apply_pipeline(img, {"enabled": False})
    assert out is img
