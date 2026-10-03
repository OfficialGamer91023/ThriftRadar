"""RapidOCR (ONNX Runtime) and size-tag parsing. Spec: DESIGN.md §4.4 `Ocr`, `parse_size_tag`."""

import re
import threading
from dataclasses import dataclass
from decimal import Decimal

import numpy as np
from PIL import Image

from app.pipeline.caption import _BRAND_RX, _first_match, normalize

MAX_SIDE = 1280
MIN_CONF = 0.6
SYSTEM_PREFERENCE = ("EU", "UK", "US", "CM", "JP")
_TAG = re.compile(r"\b(EUR?|UK|US|CM|JP)\s*[:.]?\s*(\d{1,2}(?:[.,]5)?)\b")


@dataclass(frozen=True)
class SizeGuess:
    size_system: str | None
    size_value: Decimal | None
    brand: str | None  # ocr_brand


def parse_size_tag(lines: list[tuple[str, float]], min_conf: float = MIN_CONF) -> SizeGuess | None:
    """Pure. Prefers EUR, then UK, then US; a conflict inside the chosen system gives no size."""
    good = [t for t, conf in lines if conf >= min_conf]
    by_system: dict[str, set[Decimal]] = {}
    for text in good:
        for m in _TAG.finditer(text.upper()):
            system = "EU" if m.group(1).startswith("EU") else m.group(1)
            by_system.setdefault(system, set()).add(Decimal(m.group(2).replace(",", ".")))
    system = value = None
    for s in SYSTEM_PREFERENCE:
        if s in by_system:
            if len(by_system[s]) == 1:
                system, value = s, next(iter(by_system[s]))
            break
    brand = _first_match(normalize(" ".join(good)), _BRAND_RX) if good else None
    if value is None and brand is None:
        return None
    return SizeGuess(system, value, brand)


class Ocr:
    def __init__(self):
        from rapidocr import RapidOCR

        self.engine = RapidOCR(params={"Global.log_level": "error"})
        self.lock = threading.Lock()

    def read(self, img: Image.Image | np.ndarray) -> list[tuple[str, float]]:
        if isinstance(img, Image.Image):
            img = img.convert("RGB")
            img.thumbnail((MAX_SIDE, MAX_SIDE))
            img = np.asarray(img)
        with self.lock:
            result = self.engine(img)
        txts = getattr(result, "txts", None) or ()
        scores = getattr(result, "scores", None) or ()
        return [(str(t), float(s)) for t, s in zip(txts, scores)]
