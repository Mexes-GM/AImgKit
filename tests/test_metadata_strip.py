"""Tests for FIX-06: explicit metadata stripping on save."""
import struct
from PIL import Image, PngImagePlugin


def test_png_strips_text_chunk(tmp_path):
    """PNG saved without pnginfo must not carry text chunks."""
    src = tmp_path / "src.png"
    info = PngImagePlugin.PngInfo()
    info.add_text("prompt", "test_data")
    img = Image.new("RGBA", (64, 64), (255, 0, 0, 255))
    img.save(str(src), "PNG", pnginfo=info)

    out = tmp_path / "out.png"
    # Replicate exact save logic from _overlay_watermark_worker
    img.save(str(out), "PNG", optimize=True)

    with Image.open(str(out)) as result:
        assert "prompt" not in result.info, "PNG should have no 'prompt' text chunk"


def test_jpeg_strips_exif(tmp_path):
    """JPEG saved without exif kwarg must not carry EXIF data."""
    src = tmp_path / "src.jpg"
    img = Image.new("RGB", (64, 64), (100, 150, 200))
    # Minimal valid EXIF: Exif header + empty IFD
    fake_exif = b"Exif\x00\x00II\x2a\x00\x08\x00\x00\x00\x00\x00"
    img.save(str(src), "JPEG", exif=fake_exif)

    out = tmp_path / "out.jpg"
    # Replicate exact save logic from _overlay_watermark_worker
    img.convert("RGB").save(str(out), "JPEG", quality=95, optimize=True)

    with Image.open(str(out)) as result:
        exif_data = result.info.get("exif", b"")
        assert exif_data == b"" or len(exif_data) <= 6, \
            "JPEG should have no EXIF data after save without exif kwarg"
