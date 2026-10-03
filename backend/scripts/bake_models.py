"""Download, pin, bake and verify model weights. Spec: DESIGN.md §4.8 `bake_models.py`.

Run from backend/:  uv run --group bake python -m scripts.bake_models [--out DIR]
Default output: <repo>/data/models (gitignored). The Docker build uses --out /models.
Exits non-zero on any mismatch. Runtime loads only from the output dir, with no network.
"""

import argparse
import hashlib
import json
import os
import subprocess
import sys
import textwrap
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
DEFAULT_OUT = REPO / "data" / "models"

SIGLIP_ID = "google/siglip-base-patch16-224"
SIGLIP_REVISION = "7fd15f0689c79d79e38b1c2e2e2370a7bf2761ed"
YOLO_URL = "https://github.com/ultralytics/assets/releases/download/v8.4.0/yolov8s-worldv2.pt"
YOLO_SHA256 = "9b2c17ab6124a913e9b3a5c170617920d91b0f01111a8479da69f00e2cf27792"
VOCAB = ["shoe", "sneaker", "boot", "sandal", "high heel"]
BRAND_PROMPT = "a photo of {} shoes"

YOLO_FILE = "yolow-shoe.pt"
SIGLIP_DIR = "siglip"
BRAND_FILE = "brand_text.npy"
BRAND_NAMES_FILE = "brand_text.json"
MANIFEST = "manifest.json"


class BakeError(Exception):
    pass


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def fetch_yolo(out: Path) -> Path:
    raw = out / "yolov8s-worldv2.pt"
    if not raw.exists():
        tmp = raw.with_suffix(".tmp")
        urllib.request.urlretrieve(YOLO_URL, tmp)
        os.replace(tmp, raw)
    got = sha256_file(raw)
    if got != YOLO_SHA256:
        raw.unlink()
        raise BakeError(f"YOLO-World sha256 mismatch: {got}")
    return raw


def bake_yolo(raw: Path, out: Path) -> Path:
    from ultralytics import YOLOWorld

    model = YOLOWorld(str(raw))
    model.set_classes(VOCAB)
    # set_classes caches the CLIP text encoder on the model; saved with it, the file is ~13x larger
    # and can't be loaded without the clip package. The text features it computed are all we need.
    model.model.clip_model = None
    dest = out / YOLO_FILE
    model.save(str(dest))
    return dest


VERIFY_SNIPPET = textwrap.dedent("""
    import sys
    sys.modules["clip"] = None          # the baked model must not need CLIP
    import numpy as np
    from ultralytics import YOLO
    m = YOLO(sys.argv[1])
    names = [m.names[i] for i in sorted(m.names)]
    assert names == sys.argv[2].split(","), names
    m.predict(np.zeros((640, 640, 3), dtype=np.uint8), imgsz=640, verbose=False)
    print("ok")
""")


def verify_offline(yolo_path: Path) -> None:
    env = os.environ | {"HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1", "YOLO_OFFLINE": "1"}
    r = subprocess.run([sys.executable, "-c", VERIFY_SNIPPET, str(yolo_path), ",".join(VOCAB)],
                       env=env, capture_output=True, text=True, timeout=300)
    if r.returncode != 0 or r.stdout.strip().splitlines()[-1:] != ["ok"]:
        raise BakeError("offline reload of the baked YOLO-World failed:\n" + r.stderr[-2000:])


def bake_siglip(out: Path):
    from transformers import AutoModel, AutoProcessor

    dest = out / SIGLIP_DIR
    model = AutoModel.from_pretrained(SIGLIP_ID, revision=SIGLIP_REVISION)
    processor = AutoProcessor.from_pretrained(SIGLIP_ID, revision=SIGLIP_REVISION)
    model.save_pretrained(dest)
    processor.save_pretrained(dest)
    return dest


def bake_brand_text(out: Path) -> None:
    import numpy as np

    from app.pipeline.caption import BRAND_ALIASES
    from app.pipeline.embed import Embedder

    names = list(dict.fromkeys(name for _, name in BRAND_ALIASES))
    emb = Embedder(out / SIGLIP_DIR, device="cpu")
    vecs = emb.embed_text([BRAND_PROMPT.format(n) for n in names])
    np.save(out / BRAND_FILE, vecs.astype(np.float32))
    (out / BRAND_NAMES_FILE).write_text(json.dumps(names))


def warm_ocr() -> None:
    import numpy as np

    from app.pipeline.ocr import Ocr

    Ocr().read(np.full((64, 256, 3), 255, dtype=np.uint8))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m scripts.bake_models")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = ap.parse_args(argv)
    out: Path = args.out
    out.mkdir(parents=True, exist_ok=True)
    try:
        print("1/6 siglip", flush=True)
        bake_siglip(out)
        print("2/6 yolo-world download + sha256", flush=True)
        raw = fetch_yolo(out)
        print("3/6 yolo-world vocabulary", flush=True)
        yolo = bake_yolo(raw, out)
        print("4/6 offline reload check", flush=True)
        verify_offline(yolo)
        print("5/6 brand text embeddings", flush=True)
        bake_brand_text(out)
        print("6/6 ocr warm-up", flush=True)
        warm_ocr()
    except BakeError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    manifest = {
        "siglip": {"id": SIGLIP_ID, "revision": SIGLIP_REVISION},
        "yolo_world": {"url": YOLO_URL, "sha256": YOLO_SHA256, "vocab": VOCAB,
                       "baked_sha256": sha256_file(yolo)},
        "brand_prompt": BRAND_PROMPT,
    }
    (out / MANIFEST).write_text(json.dumps(manifest, indent=2))
    print(f"baked into {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
