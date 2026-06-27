"""Post-processing filters: resize, Kuwahara blur, median filter, Gaussian noise."""
from __future__ import annotations

import numpy as np
from PIL import Image
import cv2


# ---------------------------------------------------------------------------
# Resize relative
# ---------------------------------------------------------------------------
_PIL_SAMPLERS = {
    "lanczos": Image.LANCZOS,
    "bicubic": Image.BICUBIC,
    "hamming": Image.HAMMING,
    "bilinear": Image.BILINEAR,
    "box": Image.BOX,
    "nearest": Image.NEAREST,
}


def resize_relative(img: Image.Image, scale_w: float, scale_h: float,
                    method: str = "lanczos") -> Image.Image:
    if scale_w == 1.0 and scale_h == 1.0:
        return img
    sampler = _PIL_SAMPLERS.get(method.lower(), Image.LANCZOS)
    # int() truncation for pixel dimensions.
    new_w = max(1, int(img.width * scale_w))
    new_h = max(1, int(img.height * scale_h))
    return img.resize((new_w, new_h), sampler)


# ---------------------------------------------------------------------------
# Kuwahara blur
# ---------------------------------------------------------------------------
def _kuwahara_rgb_uint8(orig_img: np.ndarray, radius: int,
                        method: str = "mean") -> np.ndarray:
    """
    `orig_img` must be RGB uint8 with shape (H, W, 3).
    Returns RGB uint8 of the same shape.

    MEMORY-OPTIMISED: processes quadrants sequentially instead of
    storing all 4 at once.  For 8K images this saves ~2 GB per call.
    """
    image = orig_img.astype(np.float32, copy=False)
    H, W = image.shape[:2]
    # Convert to grayscale for variance computation.
    image_2d = cv2.cvtColor(orig_img, cv2.COLOR_RGB2GRAY).astype(np.float32, copy=False)
    squared_img = image_2d ** 2

    if method == "mean":
        kxy = np.ones(radius + 1, dtype=np.float32) / (radius + 1)
    elif method == "gaussian":
        kxy = cv2.getGaussianKernel(2 * radius + 1, -1, ktype=cv2.CV_32F)
        kxy /= kxy[radius:].sum()
        klr = np.array([kxy[:radius + 1], kxy[radius:]])
        kindexes = [[1, 1], [1, 0], [0, 1], [0, 0]]
    else:
        raise ValueError(f"unknown kuwahara method: {method}")

    shift = [(0, 0), (0, radius), (radius, 0), (radius, radius)]

    # ── Sequential processing: one quadrant at a time ──
    best_filtered = np.empty_like(image)
    best_stddev = np.full(image.shape[:2], np.inf, dtype=np.float32)
    # Reusable temporary buffers
    tmp_avg_3ch = np.empty_like(image)
    tmp_avg_2d = np.empty(image.shape[:2], dtype=np.float32)
    tmp_stddev = np.empty(image.shape[:2], dtype=np.float32)

    for k in range(4):
        if method == "mean":
            kx, ky = kxy, kxy
        else:
            kx, ky = klr[kindexes[k]]
        cv2.sepFilter2D(image,        -1, kx, ky, tmp_avg_3ch, shift[k])
        cv2.sepFilter2D(image_2d,     -1, kx, ky, tmp_avg_2d,  shift[k])
        cv2.sepFilter2D(squared_img,  -1, kx, ky, tmp_stddev,  shift[k])
        tmp_stddev -= tmp_avg_2d ** 2

        # Update best where this quadrant has lower variance
        mask = tmp_stddev < best_stddev
        best_stddev[mask] = tmp_stddev[mask]
        best_filtered[mask] = tmp_avg_3ch[mask]

    return best_filtered.astype(orig_img.dtype)


def kuwahara_blur(img: Image.Image, radius: int = 3,
                  method: str = "mean") -> Image.Image:
    if radius <= 0:
        return img
    arr = np.asarray(img)
    if arr.ndim == 2:
        rgb = np.stack([arr] * 3, axis=-1)
        out = _kuwahara_rgb_uint8(rgb, radius, method)
        return Image.fromarray(out[..., 0], mode="L")
    has_alpha = arr.shape[2] == 4
    rgb = arr[..., :3].copy()
    out = _kuwahara_rgb_uint8(rgb, radius, method)
    if has_alpha:
        return Image.fromarray(np.dstack([out, arr[..., 3]]), mode="RGBA")
    return Image.fromarray(out, mode="RGB")


# ---------------------------------------------------------------------------
# Median filter
# ---------------------------------------------------------------------------
def median_filter_image(img: Image.Image, size: int = 1) -> Image.Image:
    """Kernel size = 2*size + 1."""
    if size < 1:
        return img
    d = size * 2 + 1
    arr = np.asarray(img)

    if arr.ndim == 2:
        return Image.fromarray(cv2.medianBlur(arr, d), mode="L")

    has_alpha = arr.shape[2] == 4
    rgb = arr[..., :3]

    out = cv2.medianBlur(rgb.copy(), d)

    if has_alpha:
        return Image.fromarray(np.dstack([out, arr[..., 3]]), mode="RGBA")
    return Image.fromarray(out, mode="RGB")


# ---------------------------------------------------------------------------
# Gaussian noise
# ---------------------------------------------------------------------------
def _channel_indices(channels: str, has_alpha: bool) -> list:
    table = {
        "rgb":  [0, 1, 2], "rgba": [0, 1, 2, 3],
        "rg":   [0, 1], "rb": [0, 2], "ra": [0, 3],
        "gb":   [1, 2], "ga": [1, 3], "ba": [2, 3],
        "r":    [0], "g": [1], "b": [2], "a": [3],
    }
    idx = table.get(channels.lower(), [0, 1, 2])
    if not has_alpha:
        idx = [i for i in idx if i != 3]
    return idx


def gaussian_noise(img: Image.Image, strength: float = 0.5,
                   monochromatic: bool = False, invert: bool = False,
                   channels: str = "rgb",
                   seed=None) -> Image.Image:
    """
    Add Gaussian half-normal noise: n = |N(0,1)|;  n /= n.max();
    out = img +/- n * strength;  clip [0,1].
    """
    if strength <= 0:
        return img
    rng = np.random.default_rng(seed)
    arr = np.asarray(img).astype(np.float32) / 255.0

    if arr.ndim == 2:
        arr = arr[..., None]
    has_alpha = arr.shape[2] == 4

    idx = _channel_indices(channels, has_alpha)
    if not idx:
        return img

    sub = arr[..., idx]  # (H, W, C')
    if monochromatic and sub.shape[2] > 1:
        noise = rng.standard_normal(sub.shape[:2]).astype(np.float32)
    else:
        noise = rng.standard_normal(sub.shape).astype(np.float32)

    noise = np.abs(noise)
    m = noise.max()
    if m > 0:
        noise = noise / m

    if monochromatic and sub.shape[2] > 1:
        noise = noise[..., None].repeat(sub.shape[2], axis=-1)

    if invert:
        sub = sub - noise * strength
    else:
        sub = sub + noise * strength

    sub = np.clip(sub, 0.0, 1.0)
    arr[..., idx] = sub

    out = (arr * 255.0).astype(np.uint8)
    if out.shape[2] == 1:
        return Image.fromarray(out[..., 0], mode="L")
    if out.shape[2] == 4:
        return Image.fromarray(out, mode="RGBA")
    return Image.fromarray(out, mode="RGB")


# ---------------------------------------------------------------------------
# Sharpen (Unsharp Mask)
# ---------------------------------------------------------------------------
def sharpen(img: Image.Image, amount: float = 0.8, radius: int = 1,
            threshold: int = 3) -> Image.Image:
    """Unsharp mask: sharpened = img + amount*(img - blur(img)), threshold avoids noise amplification."""
    if amount <= 0:
        return img
    from PIL import ImageFilter
    has_alpha = img.mode == "RGBA"
    base = img.convert("RGB") if has_alpha else img
    blurred = base.filter(ImageFilter.GaussianBlur(radius))
    arr = np.asarray(base).astype(np.int16)
    blr = np.asarray(blurred).astype(np.int16)
    diff = arr - blr
    mask = np.abs(diff) >= threshold
    out = np.clip(arr + (diff * amount * mask).astype(np.int16), 0, 255).astype(np.uint8)
    result = Image.fromarray(out, "RGB")
    if has_alpha:
        result.putalpha(img.split()[3])
    return result


# ---------------------------------------------------------------------------
# HSB (Hue / Saturation / Brightness)
# ---------------------------------------------------------------------------
def hsb_adjust(img: Image.Image, hue_shift: float = 0.0,
               sat_factor: float = 1.0, val_factor: float = 1.0) -> Image.Image:
    """Shift hue (degrees), scale saturation and brightness (multipliers)."""
    if hue_shift == 0.0 and sat_factor == 1.0 and val_factor == 1.0:
        return img
    has_alpha = img.mode == "RGBA"
    arr = np.asarray(img.convert("RGB") if has_alpha else img)
    hsv = cv2.cvtColor(arr, cv2.COLOR_RGB2HSV).astype(np.float32)
    hsv[..., 0] = (hsv[..., 0] + hue_shift / 2.0) % 180.0  # OpenCV H is 0-180
    hsv[..., 1] = np.clip(hsv[..., 1] * sat_factor, 0.0, 255.0)
    hsv[..., 2] = np.clip(hsv[..., 2] * val_factor, 0.0, 255.0)
    out = cv2.cvtColor(hsv.astype(np.uint8), cv2.COLOR_HSV2RGB)
    result = Image.fromarray(out, "RGB")
    if has_alpha:
        result.putalpha(img.split()[3])
    return result


# ---------------------------------------------------------------------------
# Chromatic Aberration
# ---------------------------------------------------------------------------
def chromatic_aberration(img: Image.Image, shift: int = 1) -> Image.Image:
    """Shift R channel right and B channel left by `shift` pixels (BORDER_REPLICATE)."""
    if shift <= 0:
        return img
    has_alpha = img.mode == "RGBA"
    arr = np.asarray(img.convert("RGB") if has_alpha else img).copy()
    H, W = arr.shape[:2]
    M_r = np.float32([[1, 0,  shift], [0, 1, 0]])
    M_b = np.float32([[1, 0, -shift], [0, 1, 0]])
    arr[..., 0] = cv2.warpAffine(arr[..., 0], M_r, (W, H), borderMode=cv2.BORDER_REPLICATE)
    arr[..., 2] = cv2.warpAffine(arr[..., 2], M_b, (W, H), borderMode=cv2.BORDER_REPLICATE)
    result = Image.fromarray(arr, "RGB")
    if has_alpha:
        result.putalpha(img.split()[3])
    return result


# ---------------------------------------------------------------------------
# Vignette
# ---------------------------------------------------------------------------
def vignette(img: Image.Image, strength: float = 0.4,
             feather: float = 1.0) -> Image.Image:
    """Darken edges with a radial gradient. strength 0=none, 1=full black corners."""
    if strength <= 0:
        return img
    has_alpha = img.mode == "RGBA"
    arr = np.asarray(img.convert("RGB") if has_alpha else img).astype(np.float32)
    H, W = arr.shape[:2]
    Y, X = np.ogrid[:H, :W]
    dist = np.sqrt(((X - W / 2) / (W / 2)) ** 2 + ((Y - H / 2) / (H / 2)) ** 2)
    mask = (1.0 - np.clip(dist ** feather * strength, 0.0, 1.0))[..., None]
    out = np.clip(arr * mask, 0, 255).astype(np.uint8)
    result = Image.fromarray(out, "RGB")
    if has_alpha:
        result.putalpha(img.split()[3])
    return result


# ---------------------------------------------------------------------------
# Film Grain
# ---------------------------------------------------------------------------
def film_grain(img: Image.Image, strength: float = 0.08,
               grain_size: int = 3, monochromatic: bool = True,
               seed=None) -> Image.Image:
    """Structured film grain via low-res noise upscaled to grain_size."""
    if strength <= 0:
        return img
    rng = np.random.default_rng(seed)
    has_alpha = img.mode == "RGBA"
    arr = np.asarray(img.convert("RGB") if has_alpha else img).astype(np.float32) / 255.0
    H, W = arr.shape[:2]
    gs = max(1, grain_size)
    sh, sw = max(1, H // gs + 1), max(1, W // gs + 1)
    if monochromatic:
        small = rng.standard_normal((sh, sw)).astype(np.float32)
        grain = cv2.resize(small, (W, H), interpolation=cv2.INTER_LINEAR)
        m = np.abs(grain).max()
        if m > 0:
            grain /= m
        arr += grain[..., None] * strength
    else:
        small = rng.standard_normal((sh, sw, 3)).astype(np.float32)
        grain = cv2.resize(small, (W, H), interpolation=cv2.INTER_LINEAR)
        m = np.abs(grain).max()
        if m > 0:
            grain /= m
        arr += grain * strength
    out = np.clip(arr * 255.0, 0, 255).astype(np.uint8)
    result = Image.fromarray(out, "RGB")
    if has_alpha:
        result.putalpha(img.split()[3])
    return result


# ---------------------------------------------------------------------------
# JPEG simulation
# ---------------------------------------------------------------------------
def jpeg_sim(img: Image.Image, quality: int = 88,
             subsampling: int = 2) -> Image.Image:
    """Simulate JPEG compression artifacts by encoding to JPEG in-memory and decoding back."""
    if quality >= 100:
        return img
    from io import BytesIO
    has_alpha = img.mode == "RGBA"
    alpha = img.split()[3] if has_alpha else None
    base = img.convert("RGB")
    buf = BytesIO()
    base.save(buf, format="JPEG", quality=quality, subsampling=subsampling, optimize=True)
    buf.seek(0)
    result = Image.open(buf).copy()
    if has_alpha:
        result = result.convert("RGBA")
        result.putalpha(alpha)
    return result


# ---------------------------------------------------------------------------
# JPEG artifact removal (pre-process)
# ---------------------------------------------------------------------------
def jpeg_artifact_removal(img: Image.Image, strength: int = 5) -> Image.Image:
    """
    Remove JPEG block artifacts using Non-Local Means denoising.
    strength: filter parameter h (3=light, 5=medium, 8=strong, 10=aggressive).
    Higher values remove more noise but also soften fine detail.
    """
    if strength <= 0:
        return img
    has_alpha = img.mode == "RGBA"
    arr = np.asarray(img.convert("RGB") if has_alpha else img)
    out = cv2.fastNlMeansDenoisingColored(
        arr,
        None,
        h=float(strength),
        hColor=float(strength),
        templateWindowSize=7,
        searchWindowSize=21,
    )
    result = Image.fromarray(out, "RGB")
    if has_alpha:
        result.putalpha(img.split()[3])
    return result



DEFAULT_PIPELINE = {
    "enabled": False,
    "upscale": 1.0,
    "upscale_method": "lanczos",
    # pre-process
    "jpeg_removal_strength": 0,
    "kuwahara_radius": 0,
    "kuwahara_method": "mean",
    "median_size": 0,
    "downscale": 1.0,
    "downscale_method": "lanczos",
    # new effects
    "sharpen_amount": 0.0,
    "sharpen_radius": 1,
    "sharpen_threshold": 3,
    "hsb_hue": 0.0,
    "hsb_sat": 1.0,
    "hsb_val": 1.0,
    "chroma_shift": 0,
    "vignette_strength": 0.0,
    "vignette_feather": 1.0,
    "jpeg_quality": 100,
    "jpeg_subsampling": 2,
    "grain_strength": 0.0,
    "grain_size": 3,
    "grain_mono": True,
    # existing noise
    "noise_strength": 0.0,
    "noise_monochromatic": True,
    "noise_invert": False,
    "noise_channels": "rgb",
}


def apply_pipeline(img: Image.Image, cfg: dict) -> Image.Image:
    if not cfg.get("enabled", True):
        return img
    out = img
    # 0. JPEG artifact removal (pre-process)
    jr = int(cfg.get("jpeg_removal_strength", 0))
    if jr > 0:
        out = jpeg_artifact_removal(out, jr)
    # 1. Upscale
    s_up = float(cfg.get("upscale", 2.0))
    if s_up != 1.0:
        out = resize_relative(out, s_up, s_up, cfg.get("upscale_method", "lanczos"))
    # 2. Kuwahara
    r = int(cfg.get("kuwahara_radius", 2))
    if r > 0:
        out = kuwahara_blur(out, r, cfg.get("kuwahara_method", "mean"))
    # 3. Median
    m = int(cfg.get("median_size", 1))
    if m > 0:
        out = median_filter_image(out, m)
    # 4. Downscale
    s_dn = float(cfg.get("downscale", 0.5))
    if s_dn != 1.0:
        out = resize_relative(out, s_dn, s_dn, cfg.get("downscale_method", "lanczos"))
    # 5. Sharpen
    sa = float(cfg.get("sharpen_amount", 0.0))
    if sa > 0:
        out = sharpen(out, sa, int(cfg.get("sharpen_radius", 1)),
                      int(cfg.get("sharpen_threshold", 3)))
    # 6. HSB
    hue = float(cfg.get("hsb_hue", 0.0))
    sat = float(cfg.get("hsb_sat", 1.0))
    val = float(cfg.get("hsb_val", 1.0))
    if hue != 0.0 or sat != 1.0 or val != 1.0:
        out = hsb_adjust(out, hue, sat, val)
    # 7. Chromatic aberration
    cs = int(cfg.get("chroma_shift", 0))
    if cs > 0:
        out = chromatic_aberration(out, cs)
    # 8. Vignette
    vs = float(cfg.get("vignette_strength", 0.0))
    if vs > 0:
        out = vignette(out, vs, float(cfg.get("vignette_feather", 1.0)))
    # 9. JPEG simulation
    jq = int(cfg.get("jpeg_quality", 100))
    if jq < 100:
        out = jpeg_sim(out, jq, int(cfg.get("jpeg_subsampling", 2)))
    # 10. Film grain
    gs = float(cfg.get("grain_strength", 0.0))
    if gs > 0:
        out = film_grain(out, gs, int(cfg.get("grain_size", 3)),
                         bool(cfg.get("grain_mono", True)))
    # 11. Gaussian noise
    ns = float(cfg.get("noise_strength", 0.08))
    if ns > 0:
        out = gaussian_noise(out, strength=ns,
                             monochromatic=bool(cfg.get("noise_monochromatic", True)),
                             invert=bool(cfg.get("noise_invert", False)),
                             channels=str(cfg.get("noise_channels", "rgb")))
    return out
