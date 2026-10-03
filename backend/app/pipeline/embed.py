"""SigLIP image and text embeddings. Spec: DESIGN.md §4.4 `Embedder`."""

import threading
from pathlib import Path

import numpy as np
from PIL import Image

BATCH = 16
DIM = 768


def l2_normalize(x: np.ndarray) -> np.ndarray:
    norms = np.linalg.norm(x, axis=-1, keepdims=True)
    return x / np.maximum(norms, 1e-12)


def segment_embedding(vecs: np.ndarray) -> np.ndarray:
    """Mean of the crop embeddings, renormalized."""
    return l2_normalize(np.asarray(vecs, dtype=np.float32).mean(axis=0))


def _features(out):
    # transformers may return a tensor or a ModelOutput depending on version
    return out if hasattr(out, "detach") else out.pooler_output


class Embedder:
    def __init__(self, model_dir: str | Path, device: str | None = None):
        import torch
        from transformers import AutoModel, AutoProcessor

        self._torch = torch
        self.device = device or ("mps" if torch.backends.mps.is_available() else "cpu")
        self.model = AutoModel.from_pretrained(str(model_dir)).to(self.device).eval()
        self.processor = AutoProcessor.from_pretrained(str(model_dir))
        self.lock = threading.Lock()

    def embed_images(self, imgs: list[Image.Image]) -> np.ndarray:
        if not imgs:
            return np.zeros((0, DIM), dtype=np.float32)
        out = []
        with self.lock, self._torch.inference_mode():
            for i in range(0, len(imgs), BATCH):
                batch = [im.convert("RGB") for im in imgs[i:i + BATCH]]
                inputs = self.processor(images=batch, return_tensors="pt").to(self.device)
                feats = _features(self.model.get_image_features(**inputs))
                out.append(feats.float().cpu().numpy())
        return l2_normalize(np.concatenate(out)).astype(np.float32)

    def embed_text(self, texts: list[str]) -> np.ndarray:
        if not texts:
            return np.zeros((0, DIM), dtype=np.float32)
        out = []
        with self.lock, self._torch.inference_mode():
            for i in range(0, len(texts), BATCH):
                # SigLIP was trained with max_length padding; other padding hurts retrieval
                inputs = self.processor(text=[t.lower() for t in texts[i:i + BATCH]], padding="max_length",
                                        truncation=True, return_tensors="pt").to(self.device)
                feats = _features(self.model.get_text_features(**inputs))
                out.append(feats.float().cpu().numpy())
        return l2_normalize(np.concatenate(out)).astype(np.float32)
