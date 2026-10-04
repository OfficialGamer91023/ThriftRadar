"""Text and image search. Spec: DESIGN.md §4.7 `GET /api/search`, `POST /api/search/image`.
Demo: a session and a per-IP rate limit; results are seed listings plus the session's own uploads."""

import io

import numpy as np
import psycopg
from fastapi import APIRouter, Query, Request, UploadFile
from fastapi.responses import JSONResponse
from PIL import Image

from app import db
from app.api.listings_view import LISTING_COLS, listing_json, visibility_sql
from app.images import InvalidImage, normalize_image
from app.matching import VLM_SIZE_SLACK, brand_family_sql, semantic_text, wishlist_filters
from app.pipeline.detect import primary_box
from app.ratelimit import guard, ip
from app.sessions import viewer

router = APIRouter()
MAX_Q = 200
MAX_LIMIT = 100


def _models_or_503(request: Request):
    models = request.app.state.models
    if not models.wait_ready(timeout=30):
        return None
    return models


def search_listings(conn: psycopg.Connection, emb: np.ndarray | None, filters: dict, limit: int,
                    owner: str | None) -> list:
    """Visible listings passing the wishlist filter rules (unknown size or price passes). With a size filter: exact
    sizes first, then AI-read sizes within the slack, then unknown sizes. Within each, by similarity when there is an
    embedding, else newest first. An exact scan."""
    where, args = [visibility_sql("l", owner)], {"emb": emb, "limit": limit, "owner": owner}
    fit = ""  # with a size filter: exact sizes first, then AI-read sizes within the slack, then unknown
    if filters.get("size_eu_min") is not None:
        where.append(f"""(l.size_eu IS NULL OR l.size_eu BETWEEN %(smin)s - (CASE WHEN l.attr_sources->>'size' = 'vlm'
                         THEN {VLM_SIZE_SLACK} ELSE 0 END) AND %(smax)s + (CASE WHEN l.attr_sources->>'size' = 'vlm'
                         THEN {VLM_SIZE_SLACK} ELSE 0 END))""")
        args.update(smin=filters["size_eu_min"], smax=filters["size_eu_max"])
        fit = ("CASE WHEN l.size_eu BETWEEN %(smin)s AND %(smax)s THEN 0 "
               "WHEN l.size_eu IS NOT NULL THEN 1 ELSE 2 END, ")
    if filters.get("max_price") is not None:
        where.append("(l.price_amount IS NULL OR l.price_amount <= %(max_price)s)")
        args["max_price"] = filters["max_price"]
    if filters.get("brand"):
        where.append(f"{brand_family_sql('l.brand')} = {brand_family_sql('%(brand)s')}")
        args["brand"] = filters["brand"]
    score = "1 - (l.embedding <=> %(emb)s)" if emb is not None else "NULL::float"
    order = "score DESC" if emb is not None else "l.last_seen_at DESC"
    return conn.execute(
        f"""SELECT {LISTING_COLS}, {score} AS score
            FROM listings l LEFT JOIN images ci ON ci.id = l.cover_image_id
            WHERE l.status = 'active' AND {" AND ".join(where)}
            ORDER BY {fit}{order}, l.id DESC
            LIMIT %(limit)s""", args).fetchall()


@router.get("/api/search")
def search(request: Request, q: str | None = Query(None, max_length=MAX_Q), size: float | None = None,
           max_price: int | None = None, brand: str | None = None, limit: int = Query(48, ge=1, le=MAX_LIMIT)):
    v = viewer(request)
    if isinstance(v, JSONResponse):
        return v
    if limited := guard(request, "search", ip(request)):
        return limited
    q = (q or "").strip() or None
    filters = wishlist_filters(q, size=size, max_price=max_price, brand=brand)
    if q is None and not any(v is not None for v in filters.values()):
        return JSONResponse({"detail": "empty_query"}, 400)
    semantic = semantic_text(q)
    emb = None
    if semantic:
        models = _models_or_503(request)
        if models is None:
            return JSONResponse({"detail": "models_loading"}, 503)
        emb = models.embedder.embed_text([semantic])[0]  # brand, size and price are already hard filters
    with db.tx(request.app.state.pool) as conn:
        rows = search_listings(conn, emb, filters, limit, v.visible_to)
    return {"query": q, "filters": _filters_json(filters), "order": "similar" if emb is not None else "newest",
            "results": [listing_json(r) for r in rows]}


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
    v = viewer(request)
    if isinstance(v, JSONResponse):
        return v
    if limited := guard(request, "image_search", ip(request)):
        return limited
    models = _models_or_503(request)
    if models is None:
        return JSONResponse({"detail": "models_loading"}, 503)
    try:
        emb, _, _ = embed_photo(models, await file.read())  # nothing is stored, no VLM
    except InvalidImage as e:
        return JSONResponse({"detail": f"invalid_image:{e.reason}"}, 422)
    with db.tx(request.app.state.pool) as conn:
        rows = search_listings(conn, emb, {}, limit, v.visible_to)
    return {"results": [listing_json(r) for r in rows]}


def _filters_json(f: dict) -> dict:
    return {"brand": f.get("brand"), "max_price": f.get("max_price"),
            "size_eu": float(f["size_eu_min"]) if f.get("size_eu_min") is not None else None}
