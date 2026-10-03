"""YOLO-World shoe detector with a baked vocabulary. Spec: DESIGN.md §4.4 `Detector`."""

import threading
from dataclasses import dataclass
from pathlib import Path

from PIL import Image

IMGSZ = 640
CONF = 0.25
IOU = 0.5
BATCH = 8
PAD = 0.08


@dataclass(frozen=True)
class Det:
    box: tuple[float, float, float, float]  # x1, y1, x2, y2 in original pixels
    conf: float
    cls: str

    def as_json(self) -> dict:
        return {"box": [round(v, 1) for v in self.box], "conf": round(self.conf, 3), "cls": self.cls}


def _area(b) -> float:
    return max(0.0, b[2] - b[0]) * max(0.0, b[3] - b[1])


def _pad(box, w: int, h: int) -> list[int]:
    x1, y1, x2, y2 = box
    px, py = (x2 - x1) * PAD, (y2 - y1) * PAD
    return [max(0, int(x1 - px)), max(0, int(y1 - py)), min(w, int(round(x2 + px))), min(h, int(round(y2 + py)))]


def primary_box(dets: list[Det], w: int, h: int) -> tuple[list[int] | None, bool]:
    """-> (padded crop box, multi_item). ≤2 boxes: their union (a pair); >2: the largest, flagged multi_item."""
    if not dets:
        return None, False
    if len(dets) <= 2:
        boxes = [d.box for d in dets]
        union = (min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes))
        return _pad(union, w, h), False
    largest = max(dets, key=lambda d: _area(d.box))
    return _pad(largest.box, w, h), True


class Detector:
    def __init__(self, weights: str | Path, device: str | None = None):
        import torch
        from ultralytics import YOLO

        self.model = YOLO(str(weights))
        self.device = device or ("mps" if torch.backends.mps.is_available() else "cpu")
        self.names = self.model.names
        self.lock = threading.Lock()

    def detect(self, images: list[Image.Image]) -> list[list[Det]]:
        import torch

        out: list[list[Det]] = []
        with self.lock, torch.inference_mode():
            for i in range(0, len(images), BATCH):
                batch = [im.convert("RGB") for im in images[i:i + BATCH]]
                results = self.model.predict(batch, imgsz=IMGSZ, conf=CONF, iou=IOU, device=self.device,
                                             verbose=False)
                for r in results:
                    dets = []
                    for xyxy, conf, cls in zip(r.boxes.xyxy.tolist(), r.boxes.conf.tolist(), r.boxes.cls.tolist()):
                        dets.append(Det(tuple(xyxy), float(conf), self.names[int(cls)]))
                    out.append(dets)
        return out
