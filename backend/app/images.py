"""Image validation, normalization and perceptual hashing. Spec: DESIGN.md §4.2."""

import hashlib
import io
import warnings
from dataclasses import dataclass
from typing import Literal

import imagehash
from PIL import Image, ImageOps

Image.MAX_IMAGE_PIXELS = 40_000_000

MAX_FILE_BYTES = 15 * 1024 * 1024
_MASK64 = (1 << 64) - 1


class InvalidImage(Exception):
    def __init__(self, reason: Literal["too_large", "bad_type", "corrupt", "bomb"]):
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class NormalizedImage:
    jpeg: bytes
    sha256: bytes
    width: int
    height: int
    phash: int


def sniff_type(head: bytes) -> Literal["jpeg", "png", "webp"] | None:
    if head[:3] == b"\xff\xd8\xff":
        return "jpeg"
    if head[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp"
    return None


def phash64(img: Image.Image) -> int:
    """64-bit pHash as a signed int64 so it fits Postgres bigint."""
    value = int(str(imagehash.phash(img, hash_size=8)), 16)
    return value - (1 << 64) if value >= (1 << 63) else value


def hamming64(a: int, b: int) -> int:
    return bin((a ^ b) & _MASK64).count("1")


def normalize_image(data: bytes, max_side: int, max_bytes: int = MAX_FILE_BYTES) -> NormalizedImage:
    if len(data) > max_bytes:
        raise InvalidImage("too_large")
    if sniff_type(data[:16]) is None:
        raise InvalidImage("bad_type")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as probe:
                probe.verify()
            img = Image.open(io.BytesIO(data))
            img.load()
    except (Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise InvalidImage("bomb") from None
    except Exception:
        raise InvalidImage("corrupt") from None

    img = ImageOps.exif_transpose(img).convert("RGB")
    img.thumbnail((max_side, max_side))
    out = io.BytesIO()
    img.save(out, format="JPEG", quality=90)  # no exif= argument: metadata (GPS, device) is dropped
    jpeg = out.getvalue()
    return NormalizedImage(
        jpeg=jpeg,
        sha256=hashlib.sha256(jpeg).digest(),
        width=img.width,
        height=img.height,
        phash=phash64(img),
    )
