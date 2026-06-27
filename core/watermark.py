from __future__ import annotations
from PIL import Image
from typing import Tuple

WATERMARK_MIN_RELATIVE = 0.06
WATERMARK_MAX_RELATIVE = 0.50


def compute_watermark_width(ref_dim: int, size_pct: float) -> int:
    """Clamp watermark width to [MIN, MAX] relative to ref_dim."""
    target = int(ref_dim * (size_pct / 100))
    min_w = int(ref_dim * WATERMARK_MIN_RELATIVE)
    max_w = int(ref_dim * WATERMARK_MAX_RELATIVE)
    return max(min_w, min(max_w, target))


def position_for_corner(base_w: int, base_h: int, wm_w: int, wm_h: int,
                        corner: str, margin: int = 10) -> Tuple[int, int]:
    """Return (x, y) paste position for the given corner."""
    if corner == "bottom-right":
        x, y = base_w - wm_w - margin, base_h - wm_h - margin
    elif corner == "top-left":
        x, y = margin, margin
    elif corner == "top-right":
        x, y = base_w - wm_w - margin, margin
    else:  # bottom-left (default)
        x, y = margin, base_h - wm_h - margin
    return (max(0, x), max(0, y))


def apply_watermark_to_image(base: Image.Image, wm: Image.Image,
                              corner: str, margin: int = 10) -> Image.Image:
    """Composite wm onto base at corner. Returns RGBA result."""
    if base.mode != "RGBA":
        base = base.convert("RGBA")
    layer = Image.new("RGBA", base.size, (0, 0, 0, 0))
    pos = position_for_corner(base.width, base.height, wm.width, wm.height, corner, margin)
    layer.paste(wm, pos, wm)
    return Image.alpha_composite(base, layer)
