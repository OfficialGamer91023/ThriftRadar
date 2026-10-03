"""Text and image search. Spec: DESIGN.md §4.7 `GET /api/search`, `POST /api/search/image`.
Local mode in build step 10; sessions and rate limits arrive with step 13."""

import io

import numpy as np
import psycopg
from fastapi import APIRouter, Query, Request, UploadFile
from fastapi.responses import JSONResponse
from PIL import Image

from app import db
from app.api.listings_view import LISTING_COLS, listing_json, visibility_sql
from app.images import InvalidImage, normalize_image
from app.matching import VLM_SIZE_SLACK, brand_family_sql, wishlist_filters
from app.pipeline.detect import primary_box

router = APIRouter()
MAX_Q = 200
MAX_LIMIT = 50


def _models_or_503(request: Request):
    models = request.app.state.models
    if not models.wait_ready(timeout=30):
        return None
    return models


def search_listings(conn: psycopg.Connection, emb: np.ndarray, filters: dict, limit: int, owner: str | None) -> list:
    """ANN over visible listings with the wishlist filter rules (unknown size or price passes)."""
    conn.execute("SET LOCAL hnsw.ef_search = 100")
    conn.execute("SET LOCAL hnsw.iterative_scan = relaxed_order")  # filtered ANN keeps scanning until it has k rows
    where, args = [visibility_sql("l", owner)], {"emb": emb, "limit": limit, "owner": owner}
    if filters.get("size_eu_min") is not None:
        where.append(f"""(l.size_eu IS NULL OR l.size_eu BETWEEN %(smin)s - (CASE WHEN l.attr_sources->>'size' = 'vlm'
                         THEN {VLM_SIZE_SLACK} ELSE 0 END) AND %(smax)s + (CASE WHEN l.attr_sources->>'size' = 'vlm'
                         THEN {VLM_SIZE_SLACK} ELSE 0 END))""")
        args.update(smin=filters["size_eu_min"], smax=filters["size_eu_max"])
    if filters.get("max_price") is not None:
        where.append("(l.price_amount IS NULL OR l.price_amount <= %(max_price)s)")
        args["max_price"] = filters["max_price"]
    if filters.get("brand"):
        where.append(f"{brand_family_sql('l.brand')} = {brand_family_sql('%(brand)s')}")
        args["brand"] = filters["brand"]
    rows = conn.execute(
        f"""SELECT * FROM (
              SELECT {LISTING_COLS}, 1 - (l.embedding <=> %(emb)s) AS score
              FROM listings l LEFT JOIN images ci ON ci.id = l.cover_image_id
              WHERE l.status = 'active' AND {" AND ".join(where)}
              ORDER BY l.embedding <=> %(emb)s LIMIT %(limit)s) r
            ORDER BY score DESC""", args).fetchall()
    return rows


@router.get("/api/search")
def search(request: Request, q: str = Query(..., min_length=1, max_length=MAX_Q), size: float | None = None,
           max_price: int | None = None, brand: str | None = None, limit: int = Query(24, ge=1, le=MAX_LIMIT)):
    models = _models_or_503(request)
    if models is None:
        return JSONResponse({"detail": "models_loading"}, 503)
    filters = wishlist_filters(q, size=size, max_price=max_price, brand=brand)
    emb = models.embedder.embed_text([q])[0]
    with db.tx(request.app.state.pool) as conn:
        rows = search_listings(conn, emb, filters, limit, None)
    return {"query": q, "filters": _filters_json(filters), "results": [listing_json(r) for r in rows]}


def embed_photo(models, data: bytes) -> tuple[np.ndarray, bytes, bytes]:
    """-> (embedding of the shoe crop or the full photo, normalized jpeg, sha256). Raises InvalidImage."""
    norm = normalize_image(data, max_side=1024, max_bytes=5 * 1024 * 1024)
    img = Image.open(io.BytesIO(norm.jpeg)).convert("RGB")
    dets = models.detector.detect([img])[0]
    box, _ = primary_box(dets, img.width, img.height)
    crop = img.crop(tuple(box)) if box else img
    return models.embedder.embed_images([crop])[0], norm.jpeg, norm.sha256


@router.post("/api/search/image")
async def search_image(request: Request, file: UploadFile, limit: int = Query(24, ge=1, le=MAX_LIMIT)):
    models = _models_or_503(request)
    if models is None:
        return JSONResponse({"detail": "models_loading"}, 503)
    try:
        emb, _, _ = embed_photo(models, await file.read())  # nothing is stored, no VLM
    except InvalidImage as e:
        return JSONResponse({"detail": f"invalid_image:{e.reason}"}, 422)
    with db.tx(request.app.state.pool) as conn:
        rows = search_listings(conn, emb, {}, limit, None)
    return {"results": [listing_json(r) for r in rows]}


def _filters_json(f: dict) -> dict:
    return {"brand": f.get("brand"), "max_price": f.get("max_price"),
            "size_eu": float(f["size_eu_min"]) if f.get("size_eu_min") is not None else None}
