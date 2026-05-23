"""
Extract character tag candidates from ComfyUI PNG metadata.

Parses the workflow graph stored in the PNG text chunk to find the
positive prompt, then extracts tags between LoRA triggers and the
1girl/1boy pivot for auto-naming.
"""
from __future__ import annotations

import json
import re
from typing import Any

from PIL import Image


SAMPLER_CLASSES = {
    "KSampler", "KSamplerAdvanced", "SamplerCustom", "SamplerCustomAdvanced",
    "UltimateSDUpscale", "FaceDetailer", "Efficient KSampler",
    "Efficient KSampler Advanced", "KSamplerEfficient",
}

TEXT_PASSTHROUGH_INPUTS = ("text", "Text", "string", "String", "prompt",
                           "positive", "input")

NEGATIVE_HINT_WORDS = {
    "watermark", "bad hands", "fewer digits", "lowres", "worst quality",
    "low quality", "nsfw", "explicit", "rating:explicit",
    "rating:questionable", "bad anatomy", "bad proportions",
    "extra digits", "extra fingers", "blurry", "jpeg artifacts",
    "deformed", "ugly", "username",
}

# `1girl`, `2girls`, `1boy`, `multiple girls`, `solo`...
COUNT_PIVOT_RE = re.compile(
    r"^(\d+(girl|boy|other)s?|multiple (girls|boys)|no humans)$",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
def read_comfy_prompt(image_path: str) -> dict | None:
    """Return the parsed `prompt` graph dict, or None if unavailable."""
    try:
        with Image.open(image_path) as im:
            raw = im.info.get("prompt")
            if not raw:
                return None
            return json.loads(raw)
    except Exception:
        return None


def _collect_text_strings(graph: dict) -> list[tuple[str, str]]:
    """Return list of (node_id, text) for every node whose inputs include a
    plain string in a known text field."""
    out: list[tuple[str, str]] = []
    for nid, node in graph.items():
        inputs = node.get("inputs", {}) or {}
        for k in TEXT_PASSTHROUGH_INPUTS:
            v = inputs.get(k)
            if isinstance(v, str) and len(v) > 5:
                out.append((nid, v))
                break
    return out


def _resolve_text(graph: dict, ref: Any, _depth: int = 0) -> str | None:
    """Follow [node_id, slot] references until a literal string is reached."""
    if _depth > 20:
        return None
    if isinstance(ref, str):
        return ref
    if not (isinstance(ref, list) and len(ref) >= 1):
        return None
    nid = str(ref[0])
    node = graph.get(nid)
    if not node:
        return None
    inputs = node.get("inputs", {}) or {}
    for k in TEXT_PASSTHROUGH_INPUTS:
        v = inputs.get(k)
        if isinstance(v, str):
            return v
        if isinstance(v, list):
            r = _resolve_text(graph, v, _depth + 1)
            if r:
                return r
    return None


def extract_positive_prompt(graph: dict) -> str | None:
    """Best-effort extraction of the positive prompt string."""
    if not graph:
        return None

    # 1) walk from sampler.positive
    for nid, node in graph.items():
        if node.get("class_type") in SAMPLER_CLASSES:
            pos_ref = (node.get("inputs", {}) or {}).get("positive")
            if pos_ref is not None:
                txt = _resolve_text(graph, pos_ref)
                if txt:
                    return txt

    # 2) fallback: longest text that doesn't smell negative
    candidates = _collect_text_strings(graph)
    if not candidates:
        return None
    scored = []
    for nid, txt in candidates:
        low = txt.lower()
        neg_hits = sum(1 for w in NEGATIVE_HINT_WORDS if w in low)
        scored.append((neg_hits, -len(txt), nid, txt))
    scored.sort()
    return scored[0][3]


# ---------------------------------------------------------------------------
def _split_tags(prompt: str) -> list[str]:
    # Split by commas that aren't escaped \( \), but ComfyUI prompts use
    # `\(` to escape the paren WITHIN a tag; commas always separate tags.
    return [t.strip() for t in prompt.split(",") if t.strip()]


def extract_character_candidates(prompt: str,
                                 max_candidates: int = 8) -> list[str]:
    """Extract candidate character tags from before the 1girl/1boy pivot."""
    tags = _split_tags(prompt)
    if not tags:
        return []

    # Locate pivot (`1girl`, `2girls`, `1boy`, etc.)
    pivot_idx = None
    for i, t in enumerate(tags):
        if COUNT_PIVOT_RE.match(t.strip()):
            pivot_idx = i
            break

    pre_pivot = tags[:pivot_idx] if pivot_idx is not None else tags[:15]

    # Drop obvious LoRA / quality / single-word body descriptors.  Rank
    # by likelihood of being a character: prefer multi-word, longer tags
    # that don't look like simple body words.
    BODY_NOISE = {
        "lips", "nose", "wide hips", "thick thighs", "narrow waist",
        "large breasts", "medium breasts", "small breasts", "huge breasts",
        "flat chest", "curvy", "plump", "slim", "muscular", "tall",
    }

    candidates: list[str] = []
    seen = set()
    for t in pre_pivot:
        tl = t.lower()
        if tl in seen:
            continue
        seen.add(tl)
        if tl in BODY_NOISE:
            continue
        # LoRA triggers tend to be short alphanumeric blobs without spaces
        if " " not in t and len(t) <= 7 and re.search(r"\d", t):
            continue
        # Ignore single short word tags (likely body parts)
        if " " not in t and len(t) <= 4:
            continue
        candidates.append(t)

    # Rank: multi-word first, then longer
    candidates.sort(key=lambda x: (-(" " in x), -len(x)))
    return candidates[:max_candidates]


# ---------------------------------------------------------------------------
def sanitize_for_filename(name: str) -> str:
    """Make a tag safe to embed in a filename."""
    # drop ComfyUI-style escape backslashes
    s = name.replace("\\(", "(").replace("\\)", ")")
    s = re.sub(r"[\\/:*?\"<>|]", "", s)
    s = s.strip().replace(" ", "_")
    return s


def get_candidates_for_image(image_path: str) -> tuple[str | None, list[str]]:
    """Convenience: returns (positive_prompt, candidate_tags)."""
    graph = read_comfy_prompt(image_path)
    if not graph:
        return None, []
    prompt = extract_positive_prompt(graph)
    if not prompt:
        return None, []
    return prompt, extract_character_candidates(prompt)
