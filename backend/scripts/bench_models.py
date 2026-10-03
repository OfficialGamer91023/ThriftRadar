"""Measure model latency and memory. Spec: DESIGN.md §4.8 `bench_models.py`; results go into §3.2.

Run from backend/:  uv run python -m scripts.bench_models [--images DIR] [--n 20] [--detector-device cpu|mps]
Default images: backend/seed/images if present, else a deterministic sample of data/media. Prints numbers only.
"""

import argparse
import random
import resource
import statistics
import sys
import time
from pathlib import Path

from PIL import Image

REPO = Path(__file__).resolve().parents[2]
SEED_IMAGES = REPO / "backend" / "seed" / "images"
MEDIA = REPO / "data" / "media"
WARMUP = 3


def rss_gb() -> float:
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return peak / (1 << 30) if sys.platform == "darwin" else peak / (1 << 20)  # bytes on macOS, KiB on Linux


def pick_images(directory: Path | None, n: int) -> list[Path]:
    if directory is None:
        directory = SEED_IMAGES if SEED_IMAGES.is_dir() else MEDIA
    files = sorted(p for p in directory.rglob("*") if p.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp"))
    if len(files) < n + WARMUP:
        raise SystemExit(f"need at least {n + WARMUP} images in {directory}")
    return random.Random(7).sample(files, n + WARMUP)


def timed(fn, items) -> list[float]:
    for x in items[:WARMUP]:
        fn(x)
    out = []
    for x in items[WARMUP:]:
        t = time.perf_counter()
        fn(x)
        out.append((time.perf_counter() - t) * 1000)
    return out


def row(name: str, ms: list[float]) -> str:
    q = statistics.quantiles(ms, n=20)
    return f"{name:34} p50 {statistics.median(ms):7.0f} ms   p95 {q[18]:7.0f} ms   RSS so far {rss_gb():.2f} GB"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m scripts.bench_models")
    ap.add_argument("--images", type=Path)
    ap.add_argument("--n", type=int, default=20)
    ap.add_argument("--models-dir", type=Path, default=REPO / "data" / "models")
    ap.add_argument("--detector-device", default=None, help="default: mps if available, else cpu")
    ap.add_argument("--embedder-device", default=None, help="default: mps if available, else cpu")
    ap.add_argument("--threads", type=int, default=None)
    args = ap.parse_args(argv)

    import torch

    from app.images import normalize_image
    from app.pipeline.detect import Detector, primary_box
    from app.pipeline.embed import Embedder
    from app.pipeline.ocr import Ocr

    if args.threads:
        torch.set_num_threads(args.threads)
    paths = pick_images(args.images, args.n)
    raw = [p.read_bytes() for p in paths]
    print(f"images: {len(paths) - WARMUP} (+{WARMUP} warm-up), torch threads {torch.get_num_threads()}")
    print(f"baseline RSS {rss_gb():.2f} GB")

    print(row("normalize + pHash (2048 px)", timed(lambda b: normalize_image(b, 2048), raw)))
    imgs = [Image.open(__import__("io").BytesIO(normalize_image(b, 2048).jpeg)).convert("RGB") for b in raw]

    t = time.perf_counter()
    det = Detector(args.models_dir / "yolow-shoe.pt", device=args.detector_device)
    print(f"detector load {time.perf_counter() - t:.1f}s on {det.device}")
    print(row("YOLO-World detect (1 image)", timed(lambda im: det.detect([im]), imgs)))

    t = time.perf_counter()
    emb = Embedder(args.models_dir / "siglip", device=args.embedder_device)
    print(f"embedder load {time.perf_counter() - t:.1f}s on {emb.device}")
    print(row("SigLIP image embed (1 crop)", timed(lambda im: emb.embed_images([im]), imgs)))
    print(row("SigLIP text embed (1 query)", timed(lambda s: emb.embed_text([s]), ["white sneakers size 42"] * len(imgs))))

    t = time.perf_counter()
    ocr = Ocr()
    print(f"ocr load {time.perf_counter() - t:.1f}s")
    print(row("RapidOCR (≤1280 px)", timed(ocr.read, imgs)))

    def local_stage(i: int) -> None:
        post = [imgs[(i + k) % len(imgs)] for k in range(3)]
        dets = det.detect(post)
        crops = []
        for im, d in zip(post, dets):
            box, _ = primary_box(d, im.width, im.height)
            crops.append(im.crop(box) if box else im)
        emb.embed_images(crops)
        ocr.read(post[0])

    print(row("local stage, 3-image post (+1 OCR)", timed(local_stage, list(range(len(imgs))))))
    found = sum(1 for d in det.detect(imgs[WARMUP:]) if d)
    print(f"detector found a shoe in {found}/{len(imgs) - WARMUP} images")
    print(f"peak RSS {rss_gb():.2f} GB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
