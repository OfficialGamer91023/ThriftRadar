"""Compare VLM models on backfill listings whose brand/size we already know. Spec: DESIGN.md §4.8 `eval_vlm.py`.

Run from backend/:
  uv run python -m scripts.eval_vlm [--n 30] [--models A,B,C]          # dry run: the plan and the call count
  uv run python -m scripts.eval_vlm --n 30 --yes                         # spends: n calls per model, no retries

Each model sees only the photos (no seller text, no OCR, nothing known), so the score is what the VLM adds on its
own. Truth comes from the seller's text or a size tag read by OCR. Calls bypass the ledger (they belong to no
pipeline run) but are capped by --n; per-item results go to data/eval/vlm_eval.json (gitignored). Prints numbers only.
"""

import argparse
import json
import random
import statistics
import sys
import time
from pathlib import Path

import httpx
import psycopg
from pgvector.psycopg import register_vector

from app.media_store import LocalDirStore
from app.pipeline.budget import estimate_cost
from app.pipeline.vlm import BadJson, SegCtx, build_request, get_backend, image_token_floor, item_attrs, parse_vlm_json
from app.settings import Settings
from app.worker import VlmCtx, _vlm_crops

REPO = Path(__file__).resolve().parents[2]
OUT = REPO / "data" / "eval"
DEFAULT_MODELS = ["Qwen/Qwen3-VL-32B-Instruct", "Qwen/Qwen3-VL-8B-Instruct", "Qwen/Qwen2.5-VL-72B-Instruct"]


def featherless_prices() -> dict[str, tuple[float, float]]:
    try:
        data = httpx.get("https://api.featherless.ai/v1/models", timeout=60).json()
        return {m["id"]: (float(m["pricing"]["input"]), float(m["pricing"]["output"])) for m in data.get("data", data)}
    except Exception:
        return {}


def pick(conn, n: int, seed: int = 11) -> list[dict]:
    """Listings with a text- or OCR-derived size first (size is the big gap), then brand-only ones."""
    rows = conn.execute(
        """SELECT l.id, l.post_id, l.segment_idx, l.brand, l.attr_sources->>'brand', l.size_label, l.size_eu,
                  l.attr_sources->>'size'
           FROM listings l
           WHERE l.item_idx = 0 AND (l.attr_sources->>'brand' IN ('caption', 'ocr')
                                     OR l.attr_sources->>'size' IN ('caption', 'ocr'))
           ORDER BY l.id""").fetchall()
    rng = random.Random(seed)
    with_size = [r for r in rows if r[7] in ("caption", "ocr")]
    brand_only = [r for r in rows if r[7] not in ("caption", "ocr")]
    chosen = rng.sample(with_size, min(len(with_size), (2 * n) // 3))
    chosen += rng.sample(brand_only, min(len(brand_only), n - len(chosen)))
    out = []
    for r in chosen:
        imgs = conn.execute(
            """SELECT id, seq, sha256, segment_idx, primary_box, detections, ocr_text FROM images
               WHERE post_id = %s AND segment_idx = %s ORDER BY seq""", (r[1], r[2])).fetchall()
        out.append({
            "listing_id": r[0],
            "brand": r[3] if r[4] in ("caption", "ocr") else None,
            "size_eu": float(r[6]) if r[7] in ("caption", "ocr") and r[6] is not None else None,
            "size_label": r[5] if r[7] in ("caption", "ocr") else None,
            "images": [{"id": i[0], "seq": i[1], "sha256": bytes(i[2]), "segment_idx": i[3], "primary_box": i[4],
                        "detections": i[5], "ocr_text": i[6]} for i in imgs]})
    return out


# Sub-brands count as their parent: a caption saying "Nike" for a Jordan is not a wrong label.
BRAND_FAMILY = {"jordan": "nike", "yeezy": "adidas"}


def _family(brand: str) -> str:
    b = brand.lower()
    return BRAND_FAMILY.get(b, b)


def score(truth: dict, attrs: dict | None) -> dict:
    got_brand = (attrs or {}).get("brand")
    got_eu = (attrs or {}).get("size_eu")
    got_label = (attrs or {}).get("size_label")
    res = {"brand_answered": got_brand is not None, "size_answered": got_label is not None}
    if truth["brand"]:
        res["brand_ok"] = bool(got_brand) and _family(got_brand) == _family(truth["brand"])
    if truth["size_label"]:
        if truth["size_eu"] is not None and got_eu is not None:
            res["size_ok"] = abs(float(got_eu) - truth["size_eu"]) <= 0.5
        else:
            res["size_ok"] = (got_label or "").replace(" ", "").lower() == truth["size_label"].replace(" ", "").lower()
    return res


def run_model(model: str, items: list[dict], settings: Settings, store, price: tuple[float, float]) -> list[dict]:
    s = settings.model_copy(update={"vlm_model": model})
    backend = get_backend(s)
    vctx = VlmCtx(None, s, store, backend, None, None)
    results = []
    for k, it in enumerate(items, 1):
        crops = _vlm_crops(vctx, it["images"], need_size=True)
        ctx = SegCtx(crops, None, [], {}, ["brand", "size"])
        res = backend.complete(build_request(ctx, model))
        row = {"listing_id": it["listing_id"], "status": res.status, "http": res.http_status, "ms": res.latency_ms,
               "in_tok": res.input_tokens, "out_tok": res.output_tokens, "images": len(crops),
               "cost": estimate_cost(res.input_tokens, res.output_tokens, floor_input=image_token_floor(ctx),
                                     price_in_per_m=price[0], price_out_per_m=price[1])}
        attrs = None
        if res.status == "ok":
            try:
                out = parse_vlm_json(res.text)
                row["valid_json"] = True
                attrs = item_attrs(out.items[0]) if out.is_shoe_listing and out.items else {}
            except BadJson:
                row["valid_json"] = False
        row.update(score(it, attrs))
        row["got"] = {"brand": (attrs or {}).get("brand"), "size": (attrs or {}).get("size_label")}
        row["truth"] = {"brand": it["brand"], "size": it["size_label"]}
        results.append(row)
        print(f"  {model.split('/')[-1]:24} {k:3}/{len(items)} {res.status} {res.latency_ms} ms", flush=True)
        if res.status == "http_error" and res.http_status in (401, 402, 403):
            print(f"  stopping {model}: http {res.http_status}", flush=True)
            break
    return results


def summarize(model: str, rows: list[dict]) -> str:
    def pct(xs):
        return f"{100 * sum(xs) / len(xs):4.0f}% ({sum(xs)}/{len(xs)})" if xs else "   -"
    ok = [r for r in rows if r["status"] == "ok"]
    ms = [r["ms"] for r in ok]
    return (f"{model.split('/')[-1]:24} calls {len(rows):3}  ok {pct([r['status'] == 'ok' for r in rows])}  "
            f"json {pct([r.get('valid_json', False) for r in ok])}\n"
            f"{'':24} brand right {pct([r['brand_ok'] for r in rows if 'brand_ok' in r])}  "
            f"size right {pct([r['size_ok'] for r in rows if 'size_ok' in r])}  "
            f"size answered {pct([r['size_answered'] for r in rows])}\n"
            f"{'':24} latency p50 {statistics.median(ms) / 1000 if ms else 0:.1f}s  "
            f"max {max(ms) / 1000 if ms else 0:.1f}s  est cost/call ${statistics.mean(r['cost'] for r in rows):.5f}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m scripts.eval_vlm")
    ap.add_argument("--n", type=int, default=30)
    ap.add_argument("--models", default=",".join(DEFAULT_MODELS))
    ap.add_argument("--yes", action="store_true", help="make the calls (default: print the plan)")
    args = ap.parse_args(argv)
    settings = Settings()
    if settings.demo_mode or not settings.featherless_api_key:
        print("refusing: needs local mode and FEATHERLESS_API_KEY in backend/.env", file=sys.stderr)
        return 2
    settings = settings.model_copy(update={"vlm_provider": "featherless"})
    models = [m.strip() for m in args.models.split(",") if m.strip()]
    with psycopg.connect(settings.database_url) as conn:
        register_vector(conn)
        items = pick(conn, args.n)
    n_size = sum(1 for i in items if i["size_label"])
    n_brand = sum(1 for i in items if i["brand"])
    prices = featherless_prices()
    print(f"{len(items)} listings ({n_size} with a known size, {n_brand} with a known brand) x {len(models)} models "
          f"= at most {len(items) * len(models)} calls")
    for m in models:
        pin, pout = prices.get(m, (0.0, 0.0))
        print(f"  {m:40} ${pin}/M in, ${pout}/M out")
    if not args.yes:
        print("dry run: add --yes to make the calls")
        return 0
    store = LocalDirStore(settings.media_dir)
    report, t0 = {}, time.monotonic()
    for m in models:
        report[m] = run_model(m, items, settings, store, prices.get(m, (0.0, 0.0)))
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "vlm_eval.json").write_text(json.dumps(report, indent=1, default=str))
    print(f"\ndone in {time.monotonic() - t0:.0f}s; per-item results in {OUT / 'vlm_eval.json'}\n")
    for m in models:
        print(summarize(m, report[m]) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
