"""Wishlists. Spec: DESIGN.md §4.7 `POST /api/wishlists`, `GET` / `DELETE /api/wishlists/{id}`.
The owner is 'local' in local mode and the session id in demo."""

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field

from app import db
from app.api.listings_view import LISTING_COLS, listing_json
from app.api.search import embed_photo
from app.images import InvalidImage
from app.pipeline.embed import zero_shot_brand
from app.matching import create_wishlist, text_wishlist_query, wishlist_filters
from app.ratelimit import guard, ip
from app.sessions import viewer

router = APIRouter()


class WishlistIn(BaseModel):
    text: str = Field(min_length=1, max_length=200)
    size: float | None = Field(default=None, ge=15, le=55)
    max_price: int | None = Field(default=None, ge=0, le=10_000_000)
    brand: str | None = Field(default=None, max_length=40)
    min_score: float | None = Field(default=None, ge=-1, le=1)


def _row_json(r) -> dict:
    return {"id": r[0], "text": r[1], "image": r[2] is not None, "brand": r[3], "max_price": r[4],
            "size_eu": float(r[5]) if r[5] is not None else None, "min_score": r[6], "active": r[7],
            "created_at": r[8].isoformat(), "matches": r[9]}


WISHLIST_SQL = """SELECT w.id, w.query_text, w.ref_image_sha, w.brand, w.max_price, w.size_eu_min, w.min_score,
                         w.active, w.created_at, (SELECT count(*) FROM matches m WHERE m.wishlist_id = w.id)
                  FROM wishlists w"""


def _create(request: Request, owner: str, *, text, emb, min_score, filters, sha=None, extra=None):
    try:
        with db.tx(request.app.state.pool) as conn:
            w = create_wishlist(conn, owner=owner, embedding=emb, min_score=min_score, text=text,
                                ref_image_sha=sha, filters=filters)
            row = conn.execute(WISHLIST_SQL + " WHERE w.id = %s", (w.id,)).fetchone()
    except ValueError as e:
        return JSONResponse({"detail": str(e)}, 409)
    return JSONResponse({**_row_json(row), **(extra or {})}, 201)


@router.post("/api/wishlists")
def add_wishlist(request: Request, body: WishlistIn):
    v = viewer(request)
    if isinstance(v, JSONResponse):
        return v
    models = request.app.state.models
    if not models.wait_ready(timeout=30):
        return JSONResponse({"detail": "models_loading"}, 503)
    s = request.app.state.settings
    filters = wishlist_filters(body.text, size=body.size, max_price=body.max_price, brand=body.brand)
    to_embed, default_min = text_wishlist_query(body.text, s.match_min_text)
    emb = models.embedder.embed_text([to_embed])[0]
    min_score = body.min_score if body.min_score is not None else default_min
    return _create(request, v.owner, text=body.text, emb=emb, min_score=min_score, filters=filters)


@router.post("/api/wishlists/image")
async def add_image_wishlist(request: Request):
    """multipart: file (photo), optional size / max_price / brand / text fields."""
    v = viewer(request)
    if isinstance(v, JSONResponse):
        return v
    if limited := guard(request, "image_search", ip(request)):  # same detector + embedder work
        return limited
    models = request.app.state.models
    if not models.wait_ready(timeout=30):
        return JSONResponse({"detail": "models_loading"}, 503)
    form = await request.form(max_files=1, max_fields=4)
    upload = form.get("file")
    if upload is None or isinstance(upload, str):
        return JSONResponse({"detail": "file_required"}, 422)
    try:
        emb, jpeg, sha = embed_photo(models, await upload.read())
    except InvalidImage as e:
        return JSONResponse({"detail": f"invalid_image:{e.reason}"}, 422)
    store = request.app.state.media_store
    if store is not None and store.writable:
        store.put(sha, jpeg)
    text = (form.get("text") or None) and str(form.get("text"))[:200]
    try:
        size = float(form["size"]) if form.get("size") else None
        max_price = int(form["max_price"]) if form.get("max_price") else None
    except ValueError:
        return JSONResponse({"detail": "bad_filter"}, 422)
    s = request.app.state.settings
    filters = wishlist_filters(text, size=size, max_price=max_price, brand=form.get("brand") or None)
    # a photo from a shop or a catalogue scores lower against listings than listings do against each other, so
    # the look gate is looser (DESIGN §4.6) and the brand, read from the photo when confident, keeps it precise
    from_photo = False
    if not filters["brand"] and (brand := zero_shot_brand(models.brand_text, models.brand_names, emb,
                                                          s.siglip_brand_min_cos, s.siglip_brand_margin)):
        filters["brand"], from_photo = brand, True
    return _create(request, v.owner, text=text, emb=emb, min_score=s.match_min_image, filters=filters, sha=sha,
                   extra={"brand_from_photo": from_photo})


@router.get("/api/wishlists")
def list_wishlists(request: Request):
    v = viewer(request)
    if isinstance(v, JSONResponse):
        return v
    with db.tx(request.app.state.pool) as conn:
        rows = conn.execute(WISHLIST_SQL + " WHERE w.owner = %s ORDER BY w.id DESC", (v.owner,)).fetchall()
    return {"results": [_row_json(r) for r in rows]}


@router.get("/api/wishlists/{wishlist_id}")
def get_wishlist(request: Request, wishlist_id: int):
    v = viewer(request)
    if isinstance(v, JSONResponse):
        return v
    with db.tx(request.app.state.pool) as conn:
        row = conn.execute(WISHLIST_SQL + " WHERE w.id = %s AND w.owner = %s", (wishlist_id, v.owner)).fetchone()
        if row is None:
            return JSONResponse({"detail": "not_found"}, 404)
        matches = conn.execute(
            f"""SELECT {LISTING_COLS}, m.score FROM matches m JOIN listings l ON l.id = m.listing_id
                LEFT JOIN images ci ON ci.id = l.cover_image_id
                WHERE m.wishlist_id = %s ORDER BY m.score DESC LIMIT 100""", (wishlist_id,)).fetchall()
    out = _row_json(row)
    out["results"] = [listing_json(r) for r in matches]
    return out


@router.delete("/api/wishlists/{wishlist_id}")
def delete_wishlist(request: Request, wishlist_id: int):
    v = viewer(request)
    if isinstance(v, JSONResponse):
        return v
    with db.tx(request.app.state.pool) as conn:
        n = conn.execute("DELETE FROM wishlists WHERE id = %s AND owner = %s", (wishlist_id, v.owner)).rowcount
    return Response(status_code=204) if n else JSONResponse({"detail": "not_found"}, 404)

