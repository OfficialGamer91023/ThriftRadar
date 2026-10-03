"""Load models once, in a background thread. Spec: DESIGN.md §4.4 `ModelRegistry`."""

import json
import logging
import os
import threading
import time
from pathlib import Path

import numpy as np

log = logging.getLogger(__name__)


class ModelRegistry:
    def __init__(self, models_dir: str | Path, demo: bool):
        self.models_dir = Path(models_dir)
        self.demo = demo
        self.ready = threading.Event()
        self.failed = threading.Event()
        self.detector = None
        self.embedder = None
        self.ocr = None
        self.brand_text: np.ndarray | None = None
        self.brand_names: list[str] = []
        self._thread: threading.Thread | None = None

    def load_async(self) -> None:
        self._thread = threading.Thread(target=self._load, name="model-load", daemon=True)
        self._thread.start()

    def load(self) -> None:
        """Synchronous variant for scripts (bench, worker --drain)."""
        self._load()
        if self.failed.is_set():
            raise RuntimeError("model load failed; see log")

    def _load(self) -> None:
        t0 = time.monotonic()
        try:
            import torch

            from app.pipeline.detect import Detector
            from app.pipeline.embed import Embedder
            from app.pipeline.ocr import Ocr

            torch.set_num_threads(2 if self.demo else (os.cpu_count() or 4))
            device = "cpu" if self.demo else None  # None: mps when available
            self.detector = Detector(self.models_dir / "yolow-shoe.pt", device=device)
            self.embedder = Embedder(self.models_dir / "siglip", device=device)
            self.ocr = Ocr()
            self.brand_text = np.load(self.models_dir / "brand_text.npy")
            self.brand_names = json.loads((self.models_dir / "brand_text.json").read_text())
            self.ready.set()
            log.info("models ready in %.1fs (embedder on %s)", time.monotonic() - t0, self.embedder.device)
        except Exception:
            log.exception("model load failed")
            self.failed.set()

    def wait_ready(self, timeout: float = 60) -> bool:
        return self.ready.wait(timeout)
