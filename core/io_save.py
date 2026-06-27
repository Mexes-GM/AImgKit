from __future__ import annotations
import os
import threading
from PIL import Image


def save_without_metadata(img: Image.Image, path: str, fmt: str) -> None:
    """Save image at path without any metadata (no pnginfo/exif/icc_profile)."""
    if fmt in ("jpg", "jpeg"):
        img.convert("RGB").save(path, "JPEG", quality=95, optimize=True)
    else:
        img.save(path, "PNG", optimize=True)


def unique_save_path(
    save_path: str,
    reserved: set[str],
    lock: threading.Lock,
) -> str:
    """Return a path that doesn't exist on disk and isn't in reserved set.
    Adds suffix _1, _2... as needed. Adds chosen path to reserved under lock.
    """
    root_p, ext = os.path.splitext(save_path)
    with lock:
        candidate = save_path
        counter = 1
        while os.path.exists(candidate) or candidate in reserved:
            candidate = f"{root_p}_{counter}{ext}"
            counter += 1
        reserved.add(candidate)
    return candidate
