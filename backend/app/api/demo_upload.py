"""POST /api/demo/posts: a demo visitor simulates a WhatsApp post. Spec: DESIGN.md §4.7 `demo_upload`.
Mounted only in demo mode, and re-checked here. The upload is visible only to its session and purged after 24 h."""

import hashlib
import re
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from app import db
from app.ids import chat_ref, sender_ref
from app.images import InvalidImage, normalize_image
from app.ingest_service import ImageInput, MsgInput, PostInput, create_post
from app.ratelimit import LIMITS, ip
from app.sessions import session_id

router = APIRouter()
MAX_CAPTION = 500
UPLOAD_MAX_SIDE = 1024
CONTROL = re.compile(r"[\x00-\x09\x0b-\x1f\x7f]")  # keeps newlines


def _known_post(pool, key: str) -> int | None:
    with db.tx(pool) as conn:
        row = conn.execute("SELECT id FROM posts WHERE idempotency_key = %s", (key,)).fetchone()
    return row[0] if row else None


def _normalize(blobs: list[bytes], max_bytes: int) -> list:
    out = []
    for data in blobs:
        try:
            out.append(normalize_image(data, max_side=UPLOAD_MAX_SIDE, max_bytes=max_bytes))
        except InvalidImage:
            continue
    return out


def _over_limit(request: Request, sid: str) -> int | None:
    """Checks all three upload limits, and records a hit in each only if all allow it -> retry_after or None."""
    limiter = request.app.state.limiter
    keys = (("upload_ip", ip(request)), ("upload_session", sid), ("upload_global", "all"))
    retry = 0
    for bucket, key in keys:
        n, window = LIMITS[bucket]
        ok, after = limiter.check(bucket, key, n, window)
        if not ok:
            retry = max(retry, after)
    if retry:
        return retry
    for bucket, key in keys:
        n, window = LIMITS[bucket]
        limiter.hit(bucket, key, n, window)
    return None


@router.post("/api/demo/posts")
async def demo_upload(request: Request):
    s = request.app.state.settings
    if not s.demo_mode:
        return JSONResponse({"detail": "not_found"}, 404)
    sid = session_id(request)
    if sid is None:
        return JSONResponse({"detail": "login_required"}, 401)
    try:
        upload_id = str(uuid.UUID(request.headers.get("x-upload-id", "")))
    except ValueError:
        return JSONResponse({"detail": "bad_upload_id"}, 400)
    pool = request.app.state.pool
    key = hashlib.sha256(f"up:{sid}:{upload_id}".encode()).hexdigest()
    existing = await run_in_threadpool(_known_post, pool, key)
    if existing is not None:  # a double submit: same answer, no quota used
        return JSONResponse({"post_id": existing, "duplicate": True}, 200)
    if (retry := _over_limit(request, sid)) is not None:
        return JSONResponse({"detail": "rate_limited"}, 429, headers={"Retry-After": str(retry)})

    form = await request.form(max_files=s.upload_max_images, max_fields=3)
    try:
        files = [f for f in form.getlist("files") if not isinstance(f, str)]
        if not 1 <= len(files) <= s.upload_max_images:
            return JSONResponse({"detail": "need_1_to_4_photos"}, 422)
        blobs = []
        for f in files:
            data = await f.read(s.upload_max_bytes + 1)
            if len(data) > s.upload_max_bytes:
                return JSONResponse({"detail": "body_too_large"}, 413)
            blobs.append(data)
        caption = CONTROL.sub("", str(form.get("caption") or "")).strip()
        if len(caption) > MAX_CAPTION:
            return JSONResponse({"detail": "caption_too_long"}, 422)
    finally:
        await form.close()

    images = await run_in_threadpool(_normalize, blobs, s.upload_max_bytes)
    if not images:
        return JSONResponse({"detail": "invalid_image:none_valid"}, 422)
    store = request.app.state.media_store
    for norm in images:
        await run_in_threadpool(store.put, norm.sha256, norm.jpeg)

    now = datetime.now(timezone.utc)
    msgs = [MsgInput(f"up:{sid}:{upload_id}:{n}", "image", now, caption if n == 0 and caption else None,
                     ImageInput(norm.sha256, norm.phash, norm.width, norm.height, len(norm.jpeg)))
            for n, norm in enumerate(images)]  # the caption rides on the first photo, as in WhatsApp
    post = PostInput(source="demo_upload", idempotency_key=key, chat_ref=chat_ref("demo", s.sender_hmac_key),
                     sender_ref=sender_ref("session:" + sid, s.sender_hmac_key), messages=msgs,
                     vlm_policy="auto", owner_session=sid)

    def _create():
        with db.tx(pool) as conn:
            return create_post(conn, post)

    result = await run_in_threadpool(_create)
    request.app.state.wake()
    if result.status == "duplicate":  # a concurrent double submit won the race
        return JSONResponse({"post_id": result.post_id, "duplicate": True}, 200)
    return JSONResponse({"post_id": result.post_id, "rejected": len(files) - len(images)}, 202)
