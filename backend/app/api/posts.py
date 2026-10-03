"""Listings feed and detail, post status, media, system status. Spec: DESIGN.md §4.7.
Local mode in build step 10; demo sessions arrive with step 13."""

import re
from datetime import datetime

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse, Response

from app import db
from app.api.listings_view import LISTING_COLS, listing_json, media_url
from app.media_store import MediaMissing

router = APIRouter()
SHA_HEX = re.compile(r"^[0-9a-f]{64}$")


@router.get("/api/listings")
def listings(request: Request, cursor: str | None = None, limit: int = Query(24, ge=1, le=50)):
    """Newest first, keyset-paginated on (last_seen_at, id). `cursor` is the previous page's `next`."""
    args: dict = {"limit": limit}
    where = "l.status = 'active'"
    if cursor:
        try:
            ts, lid = cursor.rsplit("_", 1)
            args.update(ts=datetime.fromisoformat(ts), lid=int(lid))
        except ValueError:
            return JSONResponse({"detail": "bad_cursor"}, 400)
        where += " AND (l.last_seen_at, l.id) < (%(ts)s, %(lid)s)"
    with db.tx(request.app.state.pool) as conn:
        rows = conn.execute(
            f"""SELECT {LISTING_COLS} FROM listings l LEFT JOIN images ci ON ci.id = l.cover_image_id
                WHERE {where} ORDER BY l.last_seen_at DESC, l.id DESC LIMIT %(limit)s""", args).fetchall()
    items = [listing_json(r) for r in rows]
    nxt = f"{items[-1]['last_seen_at']}_{items[-1]['id']}" if len(items) == limit else None
    return {"results": items, "next": nxt}


@router.get("/api/listings/{listing_id}")
def listing_detail(request: Request, listing_id: int):
    with db.tx(request.app.state.pool) as conn:
        row = conn.execute(
            f"""SELECT {LISTING_COLS} FROM listings l LEFT JOIN images ci ON ci.id = l.cover_image_id
                WHERE l.id = %s""", (listing_id,)).fetchone()
        if row is None:
            return JSONResponse({"detail": "not_found"}, 404)
        out = listing_json(row)
        imgs = conn.execute(
            """SELECT i.sha256 FROM listings l JOIN images i ON i.post_id = l.post_id AND i.segment_idx = l.segment_idx
               WHERE l.id = %s ORDER BY i.seq""", (listing_id,)).fetchall()
        sightings = conn.execute(
            """SELECT seen_at, price_amount, match_kind FROM listing_sightings WHERE listing_id = %s
               ORDER BY seen_at""", (listing_id,)).fetchall()
    out["images"] = [media_url(r[0]) for r in imgs]
    out["sightings"] = [{"seen_at": s.isoformat(), "price_amount": p, "kind": k} for s, p, k in sightings]
    return out


@router.get("/api/posts/{post_id}")
def post_status(request: Request, post_id: int):
    with db.tx(request.app.state.pool) as conn:
        row = conn.execute("SELECT status, outcome FROM posts WHERE id = %s", (post_id,)).fetchone()
        if row is None:
            return JSONResponse({"detail": "not_found"}, 404)
        rows = conn.execute(
            f"""SELECT {LISTING_COLS} FROM listings l LEFT JOIN images ci ON ci.id = l.cover_image_id
                WHERE l.post_id = %s ORDER BY l.segment_idx, l.item_idx""", (post_id,)).fetchall()
    return {"id": post_id, "status": row[0], "outcome": row[1], "listings": [listing_json(r) for r in rows]}


@router.get("/media/{sha}")
def media(request: Request, sha: str):
    if not SHA_HEX.match(sha):
        return JSONResponse({"detail": "not_found"}, 404)
    raw = bytes.fromhex(sha)
    with db.tx(request.app.state.pool) as conn:
        known = conn.execute("SELECT EXISTS (SELECT 1 FROM images WHERE sha256 = %s)", (raw,)).fetchone()[0]
    store = request.app.state.media_store
    if not known or store is None:
        return JSONResponse({"detail": "not_found"}, 404)
    try:
        data = store.get(raw)
    except MediaMissing:
        return JSONResponse({"detail": "not_found"}, 404)
    return Response(data, media_type="image/jpeg",
                    headers={"Cache-Control": "private, max-age=86400, immutable"})


@router.get("/api/status")
def status(request: Request):
    state = request.app.state
    s = state.settings
    with db.tx(state.pool) as conn:
        queue = dict(conn.execute("SELECT status, count(*) FROM posts GROUP BY status").fetchall())
        calls, cost = conn.execute(
            """SELECT count(*) FILTER (WHERE status IN ('reserved', 'ok', 'bad_json', 'timeout')),
                      coalesce(sum(cost_usd), 0)
               FROM vlm_calls WHERE day = (now() AT TIME ZONE 'utc')::date""").fetchone()
        wishlists = conn.execute("SELECT count(*) FROM wishlists WHERE active").fetchone()[0]
    vlm = state.worker.vlm if state.worker is not None else None
    return {
        "queue": queue,
        "models_ready": state.models.ready.is_set(),
        "vlm": {"provider": s.vlm_provider, "model": s.vlm_model, "calls_today": calls,
                "daily_cap": s.vlm_daily_cap, "est_cost_today_usd": float(cost),
                "paused": bool(vlm and vlm.breaker.is_open())},
        "wishlists": wishlists,
    }
