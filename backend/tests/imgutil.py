"""Deterministic synthetic test images."""

import io

import numpy as np
from PIL import Image


def pattern(seed: int, size=(640, 480)) -> Image.Image:
    """Smooth random blobs: structured enough for pHash to be meaningful."""
    rng = np.random.default_rng(seed)
    small = rng.integers(0, 256, (6, 8, 3), dtype=np.uint8)
    return Image.fromarray(small).resize(size, Image.BICUBIC)


def encode(img: Image.Image, fmt="JPEG", **kw) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format=fmt, **kw)
    return buf.getvalue()
