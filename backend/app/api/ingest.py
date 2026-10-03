"""POST /ingest and GET /admin/queue (local mode only). Spec: DESIGN.md §4.3."""

import logging
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Literal

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile
from starlette.exceptions import HTTPException

from app import db
from app.ids import chat_ref, idempotency_key, sender_ref
from app.images import MAX_FILE_BYTES, InvalidImage, normalize_image
from app.ingest_service import CreateResult, ImageInput, MsgInput, OverlapConflict, PostInput, create_post

log = logging.getLogger(__name__)
router = APIRouter()

IDEM_RE = re.compile(r"^[0-9a-f]{64}$")
INGEST_MAX_SIDE = 2048
EARLIEST = datetime(2009, 1, 1, tzinfo=timezone.utc)


class IngestMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: str = Field(min_length=1, max_length=128)
    kind: Literal["image", "text"]
    sent_at: datetime
    caption: str | None = Field(default=None, max_length=4096)
    file_field: str | None = Field(default=None, max_length=16)
    missing_media: bool = False
    reject_reason: Literal["corrupt", "too_large", "bad_type", "download_failed"] | None = None

    @field_validator("sent_at")
    @classmethod
    def _aware_and_plausible(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("sent_at_naive")
        if not (EARLIEST <= v <= datetime.now(timezone.utc) + timedelta(days=1)):
            raise ValueError("sent_at_out_of_range")
        return v

    @model_validator(mode="after")
    def _file_matches_kind(self) -> "IngestMessage":
        has_file = self.file_field is not None
        if self.kind == "text" and (has_file or self.missing_media):
            raise ValueError("text_with_media")
        if self.kind == "image" and has_file == self.missing_media:
            raise ValueError("image_needs_file_xor_missing")
        return self


class IngestMeta(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source: Literal["whatsapp", "chat_export"]
    chat_id: str = Field(min_length=1, max_length=256)
    sender_id: str = Field(min_length=1, max_length=256)
    sender_alt: str | None = Field(default=None, max_length=256)
    vlm_policy: Literal["auto", "never"] = "auto"
    messages: list[IngestMessage] = Field(min_length=1, max_length=60)

    @model_validator(mode="after")
    def _messages_consistent(self) -> "IngestMeta":
        keys = [m.key for m in self.messages]
        if len(set(keys)) != len(keys):
            raise ValueError("duplicate_message_keys")
        fields = [m.file_field for m in self.messages if m.file_field]
        if len(set(fields)) != len(fields):
            raise ValueError("duplicate_file_fields")
        if not any(m.kind == "image" and not m.missing_media for m in self.messages):
            raise ValueError("no_image_message")
        return self


def _error(status: int, detail: str, **extra) -> JSONResponse:
    return JSONResponse({"detail": detail, **extra}, status_code=status)


def _duplicate(post_id: int | None) -> JSONResponse:
    return JSONResponse({"status": "duplicate", "post_id": post_id}, status_code=200)


def _known_post_id(pool, key: str) -> int | None:
    with db.tx(pool) as conn:
        row = conn.execute("SELECT id FROM posts WHERE idempotency_key = %s", (key,)).fetchone()
    return row[0] if row else None


def _known_msg_keys(pool, cref: str, keys: list[str]) -> set[str]:
    with db.tx(pool) as conn:
        rows = conn.execute(
            "SELECT msg_key FROM post_messages WHERE chat_ref = %s AND msg_key = ANY(%s)", (cref, keys)
        ).fetchall()
    return {r[0] for r in rows}


def _normalize_all(blobs: dict[str, bytes]) -> dict[str, object]:
    """field -> NormalizedImage or InvalidImage reason. Runs in the threadpool."""
    out: dict[str, object] = {}
    for f, data in blobs.items():
        try:
            out[f] = normalize_image(data, max_side=INGEST_MAX_SIDE)
        except InvalidImage as e:
            out[f] = "too_large" if e.reason == "bomb" else e.reason
    return out


def _create(pool, post: PostInput) -> CreateResult:
    with db.tx(pool) as conn:
        return create_post(conn, post)


@router.post("/ingest")
async def ingest(request: Request):
    t0 = time.monotonic()
    settings = request.app.state.settings
    pool = request.app.state.pool

    # Pre 3: header shape.
    key = request.headers.get("idempotency-key", "")
    if not IDEM_RE.match(key):
        return _error(400, "bad_idempotency_key")

    # Pre 4: whole-post dedupe before the body is parsed.
    existing = await run_in_threadpool(_known_post_id, pool, key)
    if existing is not None:
        log.info("ingest dup=key post_id=%s ms=%d", existing, (time.monotonic() - t0) * 1000)
        return _duplicate(existing)

    # Pre 5: multipart with hard limits.
    try:
        form = await request.form(max_files=settings.ingest_max_images, max_fields=4)
    except HTTPException as e:
        if "Too many" in str(e.detail):
            return _error(413, "too_many_parts")
        return _error(400, "bad_multipart")

    try:
        # Pre 6: meta.
        raw_meta = form.get("meta")
        if not isinstance(raw_meta, str):
            return _error(422, "missing_meta")
        try:
            meta = IngestMeta.model_validate_json(raw_meta)
        except ValidationError as e:
            errors = [{"loc": list(err["loc"]), "type": err["type"]}
                      for err in e.errors(include_input=False, include_url=False, include_context=False)]
            return _error(422, "invalid_meta", errors=errors)
        file_parts = [(k, v) for k, v in form.multi_items() if isinstance(v, UploadFile)]
        files = dict(file_parts)
        wanted = {m.file_field for m in meta.messages if m.file_field}
        if len(files) != len(file_parts) or set(files) != wanted:
            return _error(422, "file_fields_mismatch")

        # Pre 7: the key must be ours.
        if idempotency_key(meta.source, meta.chat_id, [m.key for m in meta.messages]) != key:
            return _error(400, "key_mismatch")

        cref = chat_ref(meta.chat_id, settings.sender_hmac_key)
        sref = sender_ref(meta.sender_alt or meta.sender_id, settings.sender_hmac_key)
        blobs: dict[str, bytes] = {}
        normalized: dict[str, object] = {}

        for attempt in (1, 2):
            # Pre 8: per-message dedupe.
            known = await run_in_threadpool(_known_msg_keys, pool, cref, [m.key for m in meta.messages])
            remaining = [m for m in meta.messages if m.key not in known]
            if not any(m.kind == "image" and not m.missing_media for m in remaining):
                log.info("ingest dup=messages ms=%d", (time.monotonic() - t0) * 1000)
                return _duplicate(None)

            # Step 9: validate and normalize (cheap CPU), only for files not done on attempt 1.
            for m in remaining:
                if m.file_field and m.file_field not in blobs:
                    upload = files[m.file_field]
                    if upload.size is not None and upload.size > MAX_FILE_BYTES:
                        blobs[m.file_field] = b""
                        normalized[m.file_field] = "too_large"
                        continue
                    blobs[m.file_field] = await upload.read(MAX_FILE_BYTES + 1)
            todo = {f: b for f, b in blobs.items() if f not in normalized}
            normalized.update(await run_in_threadpool(_normalize_all, todo))

            msgs: list[MsgInput] = []
            rejected: list[dict] = []
            for m in remaining:
                image = None
                missing, reason = m.missing_media, m.reject_reason
                if m.file_field:
                    norm = normalized[m.file_field]
                    if isinstance(norm, str):
                        missing, reason = True, norm
                        rejected.append({"key": m.key, "reason": norm})
                    else:
                        image = ImageInput(norm.sha256, norm.phash, norm.width, norm.height, len(norm.jpeg))
                msgs.append(MsgInput(m.key, m.kind, m.sent_at, m.caption, image, missing, reason))
            if not any(x.image is not None for x in msgs):
                return _error(422, "no_valid_images", rejected=rejected)

            # Step 10: media first, so a committed post never points at missing bytes.
            store = request.app.state.media_store
            for m in remaining:
                norm = normalized.get(m.file_field or "")
                if norm is not None and not isinstance(norm, str):
                    await run_in_threadpool(store.put, norm.sha256, norm.jpeg)

            # Step 11.
            post = PostInput(
                source=meta.source, idempotency_key=key, chat_ref=cref, sender_ref=sref,
                sender_jid=meta.sender_alt or meta.sender_id, vlm_policy=meta.vlm_policy, messages=msgs,
            )
            try:
                result = await run_in_threadpool(_create, pool, post)
                break
            except OverlapConflict:
                if attempt == 2:
                    return _error(503, "overlap_retry")
                log.info("ingest overlap_conflict retry=1")
    finally:
        await form.close()

    n_images = sum(1 for x in msgs if x.image is not None)
    ms = int((time.monotonic() - t0) * 1000)
    if result.status == "duplicate":
        log.info("ingest dup=race post_id=%s ms=%d", result.post_id, ms)
        return _duplicate(result.post_id)

    # Step 12.
    request.app.state.wake()
    log.info("ingest post_id=%s source=%s images=%d rejected=%d dup=0 ms=%d",
             result.post_id, meta.source, n_images, len(rejected), ms)
    return JSONResponse({"status": "accepted", "post_id": result.post_id, "rejected": rejected}, status_code=202)


@router.get("/admin/queue")
def admin_queue(request: Request):
    with db.tx(request.app.state.pool) as conn:
        rows = conn.execute("SELECT status, count(*) FROM posts GROUP BY status").fetchall()
    return {status: count for status, count in rows}
