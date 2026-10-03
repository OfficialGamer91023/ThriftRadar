"""Fake models and DB helpers for worker tests (DESIGN.md §6: FakeDetector, FakeEmbedder, FakeOcr)."""

import hashlib
import threading
from datetime import datetime, timedelta, timezone

import numpy as np

from app.images import normalize_image
from app.ingest_service import ImageInput, MsgInput, PostInput, create_post
from app.pipeline.detect import Det
from tests.imgutil import encode, pattern

T0 = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
DIM = 768


def unit(seed: int | bytes) -> np.ndarray:
    if isinstance(seed, bytes):
        seed = int.from_bytes(hashlib.sha256(seed).digest()[:8], "big")
    v = np.random.default_rng(seed).normal(size=DIM).astype(np.float32)
    return v / np.linalg.norm(v)


def near(v: np.ndarray, cos: float, seed: int = 99) -> np.ndarray:
    """A unit vector with the given cosine to v."""
    r = unit(seed)
    r = r - (r @ v) * v
    r /= np.linalg.norm(r)
    out = cos * v + np.sqrt(1 - cos * cos) * r
    return (out / np.linalg.norm(out)).astype(np.float32)


class FakeDetector:
    """One centred shoe per image unless `boxes` says otherwise (a number of boxes, or a callable)."""

    def __init__(self, boxes=1, on_call=None):
        self.boxes = boxes
        self.calls = 0
        self.on_call = on_call

    def detect(self, images):
        self.calls += 1
        if self.on_call:
            self.on_call()
        out = []
        for im in images:
            n = self.boxes(im) if callable(self.boxes) else self.boxes
            w, h = im.size
            out.append([Det((w * (0.1 + 0.05 * k), h * 0.2, w * (0.5 + 0.05 * k), h * 0.8), 0.9, "shoe")
                        for k in range(n)])
        return out


class FakeEmbedder:
    """Deterministic per image content: the same crop gives the same vector."""

    def __init__(self, on_call=None, fail_times: int = 0):
        self.calls = 0
        self.on_call = on_call
        self.fail_times = fail_times

    def embed_images(self, imgs):
        self.calls += 1
        if self.on_call:
            self.on_call()
        if self.fail_times:
            self.fail_times -= 1
            raise RuntimeError("embedder boom")
        return np.stack([unit(im.tobytes()) for im in imgs])


class FakeOcr:
    def __init__(self, lines_per_call=None):
        self.lines_per_call = list(lines_per_call or [])
        self.calls = 0

    def read(self, img):
        self.calls += 1
        return self.lines_per_call.pop(0) if self.lines_per_call else []


class FakeModels:
    def __init__(self, detector=None, embedder=None, ocr=None, brand_text=None, brand_names=()):
        self.ready = threading.Event()
        self.ready.set()
        self.failed = threading.Event()
        self.detector = detector or FakeDetector()
        self.embedder = embedder or FakeEmbedder()
        self.ocr = ocr or FakeOcr()
        self.brand_text = brand_text
        self.brand_names = list(brand_names)


def make_post(conn, store, key: str, seeds: list[int], *, text: str | None = None, caption: str | None = None,
              sender: str = "a" * 32, source: str = "chat_export", at: datetime = T0, vlm_policy: str = "auto",
              seed_attrs: dict | None = None) -> int:
    """A post with one photo per seed (pattern image), `caption` on the first photo and optional trailing text."""
    msgs = []
    for n, seed in enumerate(seeds):
        norm = normalize_image(encode(pattern(seed)), max_side=640)
        if store is not None:
            store.put(norm.sha256, norm.jpeg)
        img = ImageInput(norm.sha256, norm.phash, norm.width, norm.height, len(norm.jpeg))
        msgs.append(MsgInput(f"{key}:img{n}", "image", at + timedelta(seconds=n), caption if n == 0 else None, img))
    if text:
        msgs.append(MsgInput(f"{key}:txt", "text", at + timedelta(seconds=len(seeds)), text))
    r = create_post(conn, PostInput(source=source, idempotency_key=key, chat_ref="c" * 32, sender_ref=sender,
                                    messages=msgs, vlm_policy=vlm_policy, seed_attrs=seed_attrs))
    conn.commit()
    return r.post_id
