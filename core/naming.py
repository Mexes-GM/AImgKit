from __future__ import annotations
import os
import threading
from collections import defaultdict
from comfy_metadata import sanitize_for_filename

MAX_STEM_LEN = 120


def build_output_filename(
    image_path: str,
    autoname_map: dict[str, list[str]],
    counters: defaultdict,
    lock: threading.Lock,
) -> str:
    """Thread-safe output filename builder."""
    original = os.path.basename(image_path)
    characters = autoname_map.get(image_path)
    if not characters:
        return original
    ext = os.path.splitext(original)[1]
    safe_names = [sanitize_for_filename(c) for c in characters]
    joined = "+".join(safe_names)[:MAX_STEM_LEN]
    with lock:
        counters[joined] += 1
        n = counters[joined]
    return f"{joined}_{n}{ext}"
