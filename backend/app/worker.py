"""The in-process worker: claims, leases, the local stage and the sweeper. Spec: DESIGN.md §4.5.

CLI: python -m app.worker --drain [--vlm] [--limit N]   (in the foreground; --vlm also runs the VLM stage)
"""

import argparse
import io
import logging
import sys
import threading
import time
import uuid
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import numpy as np
import psycopg
from PIL import Image
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from app import db
from app.media_store import MediaMissing, MediaStore
from app.pipeline.budget import (
    DAILY_CAP,
    POST_CAP,
    CircuitBreaker,
    RateLimiter,
    budget_available,
    cached_response,
    estimate_cost,
    finish_vlm_call,
    next_utc_midnight,
    reserve_vlm_call,
)
from app.pipeline.caption import CaptionFields, parse_caption, to_size_eu
from app.pipeline.decide import Decision, SegFlags, needs_vlm
from app.pipeline.detect import Det, primary_box
from app.pipeline.embed import segment_embedding
from app.pipeline.merge import merge_attributes
from app.pipeline.ocr import MIN_CONF, parse_size_tag
from app.pipeline.repost import RepostHit, apply_repost, find_repost_embedding, find_repost_phash, knn_brand
from app.pipeline.segment import Segment, SegMsg, segment_post
from app.pipeline.vlm import (
    PROMPT_VERSION,
    BadJson,
    Crop,
    SegCtx,
    VlmOutput,
    build_request,
    image_token_floor,
    item_attrs,
    parse_vlm_json,
)
from app.settings import Settings

log = logging.getLogger(__name__)

SWEEP_INTERVAL_S = 60


class LostClaim(Exception):
    """Another worker owns the post now (our lease expired and it was reclaimed)."""


@dataclass(frozen=True)
class PostRow:
    id: int
    source: str
    sender_ref: str
    owner_session: str | None
    first_msg_at: datetime
    last_msg_at: datetime
    vlm_policy: str
    seed_attrs: dict | None
    claim_token: uuid.UUID
    attempts: int


@dataclass(frozen=True)
class LocalCtx:
    pool: ConnectionPool
    settings: Settings
    models: object  # ModelRegistry or a fake with detector / embedder / ocr / brand_text / brand_names
    store: MediaStore


# ---------- claims and leases ----------

def claim_local(conn: psycopg.Connection, lease_s: int, max_attempts: int) -> PostRow | None:
    row = conn.execute(
        """UPDATE posts SET status = 'processing', stage = 'local', claim_token = %s, claimed_at = now(),
                  lease_expires_at = now() + make_interval(secs => %s), attempts = attempts + 1
           WHERE id = (SELECT id FROM posts
                       WHERE status = 'received' AND attempts < %s AND next_attempt_at <= now()
                       ORDER BY priority, created_at
                       FOR UPDATE SKIP LOCKED LIMIT 1)
           RETURNING id, source, sender_ref, owner_session, first_msg_at, last_msg_at, vlm_policy, seed_attrs,
                     claim_token, attempts""",
        (uuid.uuid4(), lease_s, max_attempts),
    ).fetchone()
    return PostRow(*row) if row else None


def extend_lease(pool: ConnectionPool, post: PostRow, secs: int) -> None:
    with db.tx(pool) as conn:
        n = conn.execute(
            """UPDATE posts SET lease_expires_at = now() + make_interval(secs => %s)
               WHERE id = %s AND claim_token = %s AND status = 'processing'""",
            (secs, post.id, post.claim_token)).rowcount
    if n != 1:
        raise LostClaim(post.id)


# ---------- the local stage ----------

@dataclass(eq=False)  # identity: rows hold images and arrays
class ImgRow:
    id: int
    seq: int
    sha256: bytes
    phash: int
    pil: Image.Image | None = None
    dets: list[Det] | None = None
    box: list[int] | None = None
    emb: np.ndarray | None = None
    ocr_text: str | None = None
    ocr_ran: bool = False


@dataclass
class SegPlan:
    seg: Segment
    images: list[ImgRow]
    attrs: dict
    sources: dict
    multi_item: bool = False
    kind: str = "listed"  # listed | repost | no_shoe
    hit: RepostHit | None = None
    seg_emb: np.ndarray | None = None
    cover_id: int | None = None
    decision: Decision | None = None
    note: str = ""


def _has_price(c: CaptionFields) -> bool:
    return c.price is not None or c.price_on_request or c.price_ambiguous


def _has_size(c: CaptionFields) -> bool:
    return c.size_value is not None or c.size_label is not None or c.size_ambiguous


def caption_attrs(cap: CaptionFields, extra: CaptionFields, default_currency: str) -> tuple[dict, dict, bool]:
    """Merge the segment caption with the post's extra text per field group. -> (attrs, sources, ambiguous)."""
    p = cap if _has_price(cap) else extra
    s = cap if _has_size(cap) else extra
    size_eu, approx = to_size_eu(s.size_value, s.size_system)
    attrs = {
        "brand": cap.brand or extra.brand,
        "condition": cap.condition or extra.condition,
        "size_label": s.size_label, "size_eu": size_eu, "size_approx": approx,
        "price_amount": p.price, "currency": (p.currency or default_currency or None) if p.price else None,
        "price_on_request": p.price_on_request,
    }
    sources = {}
    if attrs["brand"]:
        sources["brand"] = "caption"
    if attrs["size_label"]:
        sources["size"] = "caption"
    if attrs["price_amount"] is not None or attrs["price_on_request"]:
        sources["price"] = "caption"
    if attrs["condition"]:
        sources["condition"] = "caption"
    return attrs, sources, p.price_ambiguous or s.size_ambiguous


def seed_attrs(sa: dict) -> tuple[dict, dict]:
    attrs = {
        "brand": sa.get("brand"), "model": sa.get("model"), "colour": sa.get("colour"),
        "condition": sa.get("condition"), "size_label": sa.get("size_label"),
        "size_eu": sa.get("size_eu"), "size_approx": False,
        "price_amount": sa.get("price"), "currency": sa.get("currency"), "price_on_request": False,
    }
    names = {"brand": "brand", "model": "model", "colour": "colour", "condition": "condition",
             "size_label": "size", "price_amount": "price"}
    sources = {key: "seed" for f, key in names.items() if attrs.get(f) is not None}
    return attrs, sources


def _load(ctx: LocalCtx, post: PostRow) -> tuple[list[Segment], str, dict[int, ImgRow]]:
    with db.tx(ctx.pool) as conn:
        msgs = conn.execute(
            "SELECT seq, kind, caption, image_id FROM post_messages WHERE post_id = %s ORDER BY seq",
            (post.id,)).fetchall()
        imgs = conn.execute("SELECT id, seq, sha256, phash FROM images WHERE post_id = %s", (post.id,)).fetchall()
    segments, extra = segment_post([SegMsg(seq, kind, caption, image_id) for seq, kind, caption, image_id in msgs])
    return segments, extra, {r[0]: ImgRow(r[0], r[1], bytes(r[2]), r[3]) for r in imgs}


def _fail(ctx: LocalCtx, post: PostRow, reason: str) -> None:
    with db.tx(ctx.pool) as conn:
        n = conn.execute(
            """UPDATE posts SET status = 'failed', claim_token = NULL, lease_expires_at = NULL, last_error = %s
               WHERE id = %s AND claim_token = %s AND status = 'processing'""",
            (reason, post.id, post.claim_token)).rowcount
    if n != 1:
        raise LostClaim(post.id)
    log.info("local post_id=%s failed=%s", post.id, reason)


def _open(data: bytes) -> Image.Image:
    img = Image.open(io.BytesIO(data))
    img.load()
    return img.convert("RGB")


def _area(box: list[int] | None) -> int:
    return (box[2] - box[0]) * (box[3] - box[1]) if box else 0


def _run_ocr(ctx: LocalCtx, plan: SegPlan) -> None:
    need_size = plan.attrs.get("size_label") is None
    order = sorted((i for i in plan.images if i.pil is not None), key=lambda i: (-_area(i.box), i.seq))
    for img in order[:ctx.settings.ocr_max_images]:
        lines = ctx.models.ocr.read(img.pil)
        img.ocr_ran = True
        good = [t for t, c in lines if c >= MIN_CONF]
        img.ocr_text = "\n".join(good)[:4000] if good else None
        guess = parse_size_tag(lines)
        if guess is None:
            continue
        if guess.brand and not plan.attrs.get("brand"):
            plan.attrs["brand"] = guess.brand
            plan.sources["brand"] = "ocr"
        if guess.size_value is not None and plan.attrs.get("size_label") is None:
            size_eu, approx = to_size_eu(guess.size_value, guess.size_system)
            value = guess.size_value
            label = str(value.normalize()) if value != value.to_integral() else str(int(value))
            plan.attrs.update(size_label=f"{guess.size_system} {label}", size_eu=size_eu, size_approx=approx)
            plan.sources["size"] = "ocr"
        if need_size and plan.attrs.get("size_label") is not None:
            break
        if not need_size and plan.attrs.get("brand"):
            break


def _zero_shot_brand(ctx: LocalCtx, emb: np.ndarray) -> str | None:
    bt = ctx.models.brand_text
    if bt is None or not len(ctx.models.brand_names):
        return None
    scores = bt @ emb
    order = np.argsort(scores)[::-1]
    top = float(scores[order[0]])
    second = float(scores[order[1]]) if len(order) > 1 else -1.0
    if top >= ctx.settings.siglip_brand_min_cos and top - second >= ctx.settings.siglip_brand_margin:
        return ctx.models.brand_names[int(order[0])]
    return None


def process_local(ctx: LocalCtx, post: PostRow) -> str | None:
    """Run the local stage for one claimed post. Returns the final status, or None if the claim was lost."""
    try:
        return _process_local(ctx, post)
    except LostClaim:
        log.warning("lost_claim post_id=%s", post.id)
        return None
    except Exception as e:
        log.exception("local post_id=%s error=%s", post.id, type(e).__name__)
        try:
            with db.tx(ctx.pool) as conn:
                conn.execute(
                    """UPDATE posts SET status = 'received', claim_token = NULL, lease_expires_at = NULL,
                              last_error = %s, next_attempt_at = now() + interval '1 min' * attempts
                       WHERE id = %s AND claim_token = %s AND status = 'processing'""",
                    (type(e).__name__[:500], post.id, post.claim_token))
        except Exception:
            log.exception("local post_id=%s could not record error; the sweeper will reclaim it", post.id)
        return "received"


def _process_local(ctx: LocalCtx, post: PostRow) -> str:
    s = ctx.settings
    t0 = time.monotonic()
    timings = {"det": 0.0, "emb": 0.0, "ocr": 0.0}

    # 1–2. Load and segment.
    segments, extra_text, images = _load(ctx, post)
    if not segments:
        _fail(ctx, post, "no_images")
        return "failed"

    # 3. Fields from text (or seed ground truth).
    is_seed = post.source == "demo_seed"
    extra = parse_caption(extra_text) if extra_text else CaptionFields()
    plans: list[SegPlan] = []
    for seg in segments:
        if is_seed:
            attrs, sources = seed_attrs(post.seed_attrs or {})
            ambiguous = False
        else:
            attrs, sources, ambiguous = caption_attrs(parse_caption(seg.caption), extra, s.default_currency)
        plans.append(SegPlan(seg, [images[i] for i in seg.image_ids], attrs, sources, multi_item=ambiguous))

    # 4. Repost guard 1 (pHash): cheap, before any media is read.
    with db.tx(ctx.pool) as conn:
        for plan in plans:
            plan.hit = find_repost_phash(
                conn, post_id=post.id, sender_ref=post.sender_ref, ref_time=post.first_msg_at,
                phashes=[i.phash for i in plan.images], window_days=s.repost_window_days,
                max_dist=s.phash_max_dist, xseller_max_dist=s.phash_xseller_max_dist)
            if plan.hit:
                plan.kind = "repost"

    todo = [p for p in plans if p.kind == "listed"]
    if todo:
        # 6. Load media.
        extend_lease(ctx.pool, post, s.lease_local_s)
        loaded = 0
        for plan in todo:
            for img in plan.images:
                try:
                    img.pil = _open(ctx.store.get(img.sha256))
                    loaded += 1
                except MediaMissing:
                    pass
        if loaded == 0:
            _fail(ctx, post, "media_missing")
            return "failed"
        for plan in todo:
            if not any(i.pil is not None for i in plan.images):
                plan.kind, plan.note = "no_shoe", "media_missing"

        # 7. Detect.
        live = [i for p in todo if p.kind == "listed" for i in p.images if i.pil is not None]
        t = time.monotonic()
        for img, dets in zip(live, ctx.models.detector.detect([i.pil for i in live])):
            img.dets = dets
            img.box, multi = primary_box(dets, img.pil.width, img.pil.height)
            if multi:
                for plan in todo:
                    if img in plan.images:
                        plan.multi_item = True
        timings["det"] = time.monotonic() - t
        for plan in todo:
            if plan.kind == "listed" and not any(i.box for i in plan.images) and not is_seed:
                plan.kind = "no_shoe"

        # 8. Embed primary crops.
        extend_lease(ctx.pool, post, s.lease_local_s)
        crops: list[tuple[ImgRow, Image.Image]] = []
        for plan in todo:
            if plan.kind != "listed":
                continue
            boxed = [i for i in plan.images if i.box]
            if boxed:
                crops += [(i, i.pil.crop(tuple(i.box))) for i in boxed]
            else:  # seed segment with no detection: embed the full images
                crops += [(i, i.pil) for i in plan.images if i.pil is not None]
        t = time.monotonic()
        vecs = ctx.models.embedder.embed_images([c for _, c in crops]) if crops else []
        timings["emb"] = time.monotonic() - t
        for (img, _), v in zip(crops, vecs):
            img.emb = np.asarray(v, dtype=np.float32)
        for plan in todo:
            if plan.kind != "listed":
                continue
            plan.seg_emb = segment_embedding(np.stack([i.emb for i in plan.images if i.emb is not None]))
            plan.cover_id = next((i.id for i in sorted(plan.images, key=lambda i: i.seq) if i.box),
                                 min(plan.images, key=lambda i: i.seq).id)

        # 9. Repost guard 2 (embedding, same seller).
        with db.tx(ctx.pool) as conn:
            for plan in todo:
                if plan.kind != "listed":
                    continue
                plan.hit = find_repost_embedding(
                    conn, post_id=post.id, sender_ref=post.sender_ref, ref_time=post.first_msg_at,
                    emb=plan.seg_emb, window_days=s.repost_window_days, min_cos=s.repost_emb_min_cos)
                if plan.hit:
                    plan.kind = "repost"

        # 10. OCR, only for what is still missing.
        for plan in todo:
            if plan.kind == "listed" and not is_seed and (
                    plan.attrs.get("size_label") is None or not plan.attrs.get("brand")):
                extend_lease(ctx.pool, post, s.lease_local_s)
                t = time.monotonic()
                _run_ocr(ctx, plan)
                timings["ocr"] += time.monotonic() - t

        # 11. Brand from the image: SigLIP zero-shot, then kNN over trusted labels.
        for plan in todo:
            if plan.kind != "listed" or plan.attrs.get("brand"):
                continue
            if brand := _zero_shot_brand(ctx, plan.seg_emb):
                plan.attrs["brand"], plan.sources["brand"] = brand, "siglip"
                continue
            with db.tx(ctx.pool) as conn:
                if knn := knn_brand(conn, post.id, plan.seg_emb):
                    plan.attrs["brand"], plan.sources["brand"] = knn[0], "knn"

    for plan in plans:
        flags = SegFlags(repost=plan.kind == "repost", no_shoe=plan.kind == "no_shoe", multi_item=plan.multi_item)
        plan.decision = needs_vlm(plan.attrs, flags, post.vlm_policy, s.vlm_provider, s.required_fields)

    # 12. One transaction; the claim check at the end rolls everything back if we lost the post.
    status = "awaiting_vlm" if any(p.decision.needed for p in plans) else "done"
    kinds = {p.kind for p in plans}
    outcome = kinds.pop() if len(kinds) == 1 else "mixed"
    listing_ids: list[int] = []
    with db.tx(ctx.pool) as conn:
        for plan in plans:
            for img in plan.images:
                conn.execute(
                    """UPDATE images SET segment_idx = %s, detections = %s, primary_box = %s, embedding = %s,
                              ocr_text = %s, ocr_ran = %s WHERE id = %s""",
                    (plan.seg.idx, Jsonb([d.as_json() for d in img.dets]) if img.dets is not None else None,
                     img.box, img.emb, img.ocr_text, img.ocr_ran, img.id))
            if plan.kind == "repost":
                apply_repost(conn, plan.hit, post_id=post.id, segment_idx=plan.seg.idx, seen_at=post.last_msg_at,
                             attrs=plan.attrs, sources=plan.sources)
            elif plan.kind == "listed":
                listing_ids.append(_upsert_listing(conn, post, plan, is_seed))
        n = conn.execute(
            """UPDATE posts SET status = %s, outcome = %s, claim_token = NULL, lease_expires_at = NULL,
                      last_error = NULL
               WHERE id = %s AND claim_token = %s AND status = 'processing'""",
            (status, outcome, post.id, post.claim_token)).rowcount
        if n != 1:
            raise LostClaim(post.id)

    # 13. Matching and notifications arrive in build step 10 (sweep_unmatched catches up).
    ms = lambda x: int(x * 1000)
    log.info("local post_id=%s segs=%d repost=%d no_shoe=%d ocr_ran=%d decision=[%s] status=%s "
             "t_det=%d t_emb=%d t_ocr=%d total_ms=%d",
             post.id, len(plans), sum(p.kind == "repost" for p in plans), sum(p.kind == "no_shoe" for p in plans),
             sum(i.ocr_ran for p in plans for i in p.images), ",".join(p.decision.reason for p in plans), status,
             ms(timings["det"]), ms(timings["emb"]), ms(timings["ocr"]), ms(time.monotonic() - t0))
    return status


def _upsert_listing(conn: psycopg.Connection, post: PostRow, plan: SegPlan, is_seed: bool) -> int:
    a = plan.attrs
    row = {
        "post_id": post.id, "segment_idx": plan.seg.idx, "item_idx": 0, "source": post.source,
        "owner_session": post.owner_session, "sender_ref": post.sender_ref,
        "brand": a.get("brand"), "model": a.get("model"), "colour": a.get("colour"),
        "condition": a.get("condition"), "size_label": a.get("size_label"), "size_eu": a.get("size_eu"),
        "size_approx": bool(a.get("size_approx")), "price_amount": a.get("price_amount"),
        "currency": a.get("currency"), "price_on_request": bool(a.get("price_on_request")),
        "attr_sources": Jsonb(plan.sources), "extraction": "seed" if is_seed else "local",
        "vlm_missing": plan.decision.missing if plan.decision.needed else None,  # [] = multi_item only
        "cover_image_id": plan.cover_id, "embedding": plan.seg_emb,
        "first_seen_at": post.last_msg_at, "last_seen_at": post.last_msg_at,
    }
    cols = list(row)
    updates = ", ".join(f"{c} = EXCLUDED.{c}" for c in cols if c not in ("post_id", "segment_idx", "item_idx"))
    listing_id = conn.execute(
        f"""INSERT INTO listings ({", ".join(cols)}) VALUES ({", ".join(f"%({c})s" for c in cols)})
            ON CONFLICT (post_id, segment_idx, item_idx) DO UPDATE SET {updates} RETURNING id""",
        row).fetchone()[0]
    conn.execute(
        """INSERT INTO listing_sightings (listing_id, post_id, segment_idx, seen_at, price_amount, match_kind)
           VALUES (%s, %s, %s, %s, %s, 'origin') ON CONFLICT (post_id, segment_idx) DO NOTHING""",
        (listing_id, post.id, plan.seg.idx, post.last_msg_at, a.get("price_amount")))
    return listing_id


# ---------- sweeper ----------

def sweep_stale_claims(conn: psycopg.Connection) -> list[int]:
    ids = [r[0] for r in conn.execute(
        """UPDATE posts SET status = CASE stage WHEN 'local' THEN 'received' ELSE 'awaiting_vlm' END,
                  claim_token = NULL, lease_expires_at = NULL, last_error = 'lease_expired'
           WHERE status = 'processing' AND lease_expires_at < now() RETURNING id""").fetchall()]
    if ids:
        log.warning("sweep stale_claims ids=%s", ids)
    return ids


def sweep_exhausted(conn: psycopg.Connection, max_attempts: int) -> list[int]:
    ids = [r[0] for r in conn.execute(
        "UPDATE posts SET status = 'failed' WHERE status = 'received' AND attempts >= %s RETURNING id",
        (max_attempts,)).fetchall()]
    if ids:
        log.warning("sweep exhausted ids=%s", ids)
    return ids


def sweep_once(pool: ConnectionPool, settings: Settings) -> None:
    with db.tx(pool) as conn:
        sweep_stale_claims(conn)
        sweep_exhausted(conn, settings.max_local_attempts)


# ---------- the VLM stage ----------

LISTING_ATTRS = ("brand", "model", "colour", "condition", "gender", "size_label", "size_eu", "size_approx",
                 "price_amount", "currency", "price_on_request")
BACKOFF_S = (30, 120, 600)
FAILED = object()  # a segment that ends as vlm_failed


@dataclass(frozen=True)
class VlmCtx:
    pool: ConnectionPool
    settings: Settings
    store: MediaStore
    backend: object  # OpenAICompatBackend / OllamaBackend / a fake with .name and .complete(req)
    breaker: CircuitBreaker
    limiter: RateLimiter


def claim_vlm(conn: psycopg.Connection, lease_s: int) -> PostRow | None:
    row = conn.execute(
        """UPDATE posts SET status = 'processing', stage = 'vlm', claim_token = %s, claimed_at = now(),
                  lease_expires_at = now() + make_interval(secs => %s), vlm_attempts = vlm_attempts + 1
           WHERE id = (SELECT id FROM posts
                       WHERE status = 'awaiting_vlm' AND next_attempt_at <= now()
                       ORDER BY priority, created_at
                       FOR UPDATE SKIP LOCKED LIMIT 1)
           RETURNING id, source, sender_ref, owner_session, first_msg_at, last_msg_at, vlm_policy, seed_attrs,
                     claim_token, vlm_attempts""",
        (uuid.uuid4(), lease_s),
    ).fetchone()
    return PostRow(*row) if row else None


def _backoff(attempt: int, retry_after: float | None = None) -> timedelta:
    base = BACKOFF_S[min(max(attempt, 1), len(BACKOFF_S)) - 1]
    return timedelta(seconds=max(base, retry_after or 0))


def _vlm_crops(ctx: VlmCtx, images: list[dict], need_size: bool) -> list[Crop]:
    """Shoe crops first, most confident detection first; when the size is missing, fill the remaining slots with
    photos that had no shoe box (often a close-up of the size tag)."""
    s = ctx.settings
    boxed = sorted((i for i in images if i["primary_box"]),
                   key=lambda i: (-max((d["conf"] for d in i["detections"] or []), default=0), i["seq"]))
    unboxed = sorted((i for i in images if not i["primary_box"]), key=lambda i: i["seq"])
    chosen = boxed[:s.vlm_max_images]
    if need_size or not chosen:
        chosen += unboxed[:s.vlm_max_images - len(chosen)]
    crops = []
    for img in chosen:
        try:
            pil = _open(ctx.store.get(img["sha256"]))
        except MediaMissing:
            continue
        if img["primary_box"]:
            pil = pil.crop(tuple(img["primary_box"]))
        pil.thumbnail((s.vlm_max_side_px, s.vlm_max_side_px))
        buf = io.BytesIO()
        pil.save(buf, format="JPEG", quality=85)
        crops.append(Crop(buf.getvalue(), pil.width, pil.height))
    return crops


def _load_vlm(ctx: VlmCtx, post: PostRow):
    with db.tx(ctx.pool) as conn:
        cols = ", ".join(LISTING_ATTRS)
        rows = conn.execute(
            f"""SELECT id, segment_idx, attr_sources, vlm_missing, embedding, cover_image_id, {cols}
                FROM listings WHERE post_id = %s AND item_idx = 0 AND extraction = 'local'
                  AND vlm_missing IS NOT NULL ORDER BY segment_idx""", (post.id,)).fetchall()
        msgs = conn.execute(
            "SELECT seq, kind, caption, image_id FROM post_messages WHERE post_id = %s ORDER BY seq",
            (post.id,)).fetchall()
        imgs = conn.execute(
            """SELECT id, seq, sha256, segment_idx, primary_box, detections, ocr_text FROM images
               WHERE post_id = %s ORDER BY seq""", (post.id,)).fetchall()
    listings = []
    for r in rows:
        listings.append({"id": r[0], "segment_idx": r[1], "sources": dict(r[2]), "missing": list(r[3]),
                         "embedding": r[4], "cover_image_id": r[5], "attrs": dict(zip(LISTING_ATTRS, r[6:]))})
    segments, extra = segment_post([SegMsg(seq, kind, caption, image_id) for seq, kind, caption, image_id in msgs])
    images = [{"id": i[0], "seq": i[1], "sha256": bytes(i[2]), "segment_idx": i[3], "primary_box": i[4],
               "detections": i[5], "ocr_text": i[6]} for i in imgs]
    return listings, {sg.idx: sg for sg in segments}, extra, images


def process_vlm(ctx: VlmCtx, post: PostRow) -> str | None:
    """The VLM stage for one claimed post. Returns the final status, or None if the claim was lost."""
    try:
        return _process_vlm(ctx, post)
    except LostClaim:
        log.warning("lost_claim stage=vlm post_id=%s", post.id)
        return None
    except Exception as e:
        log.exception("vlm post_id=%s error=%s", post.id, type(e).__name__)
        try:
            with db.tx(ctx.pool) as conn:
                conn.execute(
                    """UPDATE posts SET status = 'awaiting_vlm', claim_token = NULL, lease_expires_at = NULL,
                              last_error = %s, next_attempt_at = %s
                       WHERE id = %s AND claim_token = %s AND status = 'processing'""",
                    (type(e).__name__[:500], datetime.now(UTC) + _backoff(post.attempts),
                     post.id, post.claim_token))
        except Exception:
            log.exception("vlm post_id=%s could not record error; the sweeper will reclaim it", post.id)
        return "awaiting_vlm"


def _process_vlm(ctx: VlmCtx, post: PostRow) -> str:
    s = ctx.settings
    provider, model = ctx.backend.name, s.vlm_model
    listings, segments, extra, images = _load_vlm(ctx, post)
    results: dict[int, object] = {}  # segment_idx -> VlmOutput | FAILED
    release: tuple[datetime, str, bool] | None = None  # (next_attempt_at, reason, refund the claim's attempt)

    for lst in listings:
        seg_idx = lst["segment_idx"]
        # 2a. Response cache: a paid answer is never paid for twice.
        with db.tx(ctx.pool) as conn:
            cached = cached_response(conn, post.id, seg_idx, PROMPT_VERSION)
        if cached is not None:
            results[seg_idx] = VlmOutput.model_validate(cached)
            log.info("vlm post_id=%s seg=%s cached=1", post.id, seg_idx)
            continue

        seg_images = [i for i in images if i["segment_idx"] == seg_idx]
        need_size = "size" in lst["missing"] or not lst["attrs"].get("size_label")
        seg = segments.get(seg_idx)
        caption = "\n".join(t for t in ((seg.caption if seg else None), extra) if t) or None
        ocr = [ln for i in seg_images if i["ocr_text"] for ln in i["ocr_text"].splitlines()]
        known = {k: (str(v) if not isinstance(v, (int, float, bool, str)) else v)
                 for k, v in lst["attrs"].items() if v not in (None, False) and k != "size_eu"}
        repair = False
        crops: list[Crop] | None = None
        while True:
            if not ctx.breaker.allow():
                release = (ctx.breaker.reopen_at(), "breaker_open", True)
                break
            extend_lease(ctx.pool, post, s.lease_vlm_s)  # a worker that lost the post reserves nothing
            with db.get_conn(ctx.pool) as conn:
                call = reserve_vlm_call(conn, post_id=post.id, segment_idx=seg_idx, prompt_version=PROMPT_VERSION,
                                        provider=provider, model=model, daily_cap=s.vlm_daily_cap,
                                        max_attempts=s.vlm_max_attempts)
            if call == DAILY_CAP:
                release = (next_utc_midnight(), "daily_cap", True)
                break
            if call == POST_CAP:
                results[seg_idx] = FAILED
                break
            if crops is None:
                crops = _vlm_crops(ctx, seg_images, need_size)
            seg_ctx = SegCtx(crops, caption, ocr, known, lst["missing"])
            req = build_request(seg_ctx, model, repair=repair)
            ctx.limiter.acquire()
            res = ctx.backend.complete(req)
            out, status, excerpt = None, res.status, None
            if res.status == "ok":
                try:
                    out = parse_vlm_json(res.text)
                except BadJson as e:
                    status, excerpt = "bad_json", e.excerpt
            cost = res.provider_cost if res.provider_cost is not None else estimate_cost(
                res.input_tokens, res.output_tokens, floor_input=image_token_floor(seg_ctx),
                price_in_per_m=s.vlm_price_in_per_m, price_out_per_m=s.vlm_price_out_per_m)
            with db.get_conn(ctx.pool) as conn:
                finish_vlm_call(conn, call, status=status, http_status=res.http_status,
                                input_tokens=res.input_tokens, output_tokens=res.output_tokens,
                                cost_usd=cost if res.status == "ok" or res.status == "timeout" else 0,
                                latency_ms=res.latency_ms, response=out.model_dump() if out else None,
                                raw_excerpt=excerpt)
            log.info("vlm post_id=%s seg=%s call=%s status=%s http=%s ms=%s in_tok=%s out_tok=%s cost_usd=%s",
                     post.id, seg_idx, call, status, res.http_status, res.latency_ms, res.input_tokens,
                     res.output_tokens, cost)
            code = res.http_status
            if res.status == "http_error" and code in (401, 402, 403):
                ctx.breaker.open(900, reason=f"http_{code}")
                release = (ctx.breaker.reopen_at(), f"http_{code}", True)
                break
            ctx.breaker.success()
            if out is not None:
                results[seg_idx] = out
                break
            if status == "bad_json":
                if repair:
                    results[seg_idx] = FAILED
                    break
                repair = True
                continue
            if res.status == "http_error" and code == 400:
                log.error("vlm post_id=%s seg=%s http 400: request rejected (our bug)", post.id, seg_idx)
                results[seg_idx] = FAILED
                break
            # transient: timeout, network error, 429, 5xx
            delay = _backoff(post.attempts, res.retry_after if res.status == "http_error" else None)
            release = (datetime.now(UTC) + delay, f"{res.status}:{code or '-'}", False)
            break
        if release:
            break

    # 3. One transaction, conditional on the claim.
    affected: list[int] = []
    with db.tx(ctx.pool) as conn:
        for lst in listings:
            r = results.get(lst["segment_idx"])
            if r is None:
                continue
            if r is FAILED:
                conn.execute("UPDATE listings SET extraction = 'vlm_failed', vlm_missing = NULL WHERE id = %s",
                             (lst["id"],))
                continue
            affected += _apply_vlm(conn, post, lst, r, images)
        unresolved = any(lst["segment_idx"] not in results for lst in listings)
        status = "awaiting_vlm" if unresolved else "done"
        next_at, reason, refund = release if release else (None, None, False)
        n = conn.execute(
            """UPDATE posts SET status = %s, claim_token = NULL, lease_expires_at = NULL,
                      next_attempt_at = coalesce(%s, next_attempt_at), last_error = %s,
                      vlm_attempts = vlm_attempts - %s
               WHERE id = %s AND claim_token = %s AND status = 'processing'""",
            (status, next_at, reason, 1 if refund else 0, post.id, post.claim_token)).rowcount
        if n != 1:
            raise LostClaim(post.id)
    # 4. Matching and notifications arrive in build step 10.
    log.info("vlm post_id=%s segs=%d resolved=%d status=%s reason=%s", post.id, len(listings),
             len(results), status, release[1] if release else "-")
    return status


def _multi_flag(seg_images: list[dict]) -> bool:
    return any(len(i["detections"] or []) > 2 for i in seg_images)


def _apply_vlm(conn: psycopg.Connection, post: PostRow, lst: dict, out: VlmOutput, images: list[dict]) -> list[int]:
    items = out.items if out.is_shoe_listing else []
    ids = [lst["id"]]
    attrs, sources = lst["attrs"], lst["sources"]
    if items:
        attrs, sources = merge_attributes(lst["attrs"], lst["sources"], item_attrs(items[0]))
    sets = ", ".join(f"{c} = %({c})s" for c in LISTING_ATTRS)
    conn.execute(
        f"""UPDATE listings SET {sets}, attr_sources = %(src)s, extraction = 'vlm', vlm_missing = NULL,
                   prompt_version = %(pv)s WHERE id = %(id)s""",
        {**{c: attrs.get(c) for c in LISTING_ATTRS}, "size_approx": bool(attrs.get("size_approx")),
         "price_on_request": bool(attrs.get("price_on_request")), "src": Jsonb(sources), "pv": PROMPT_VERSION,
         "id": lst["id"]})
    seg_images = [i for i in images if i["segment_idx"] == lst["segment_idx"]]
    if len(items) > 1 and (_multi_flag(seg_images) or lst["missing"] == []):
        for k, item in enumerate(items[1:], start=1):
            a, src = merge_attributes({}, {}, item_attrs(item))
            row = {c: a.get(c) for c in LISTING_ATTRS}
            row.update(size_approx=bool(a.get("size_approx")), price_on_request=bool(a.get("price_on_request")))
            cols = list(row)
            ids.append(conn.execute(
                f"""INSERT INTO listings (post_id, segment_idx, item_idx, source, owner_session, sender_ref,
                                          {", ".join(cols)}, attr_sources, extraction, embedding, cover_image_id,
                                          first_seen_at, last_seen_at, prompt_version)
                    VALUES (%(post_id)s, %(seg)s, %(k)s, %(source)s, %(owner)s, %(sender)s,
                            {", ".join(f"%({c})s" for c in cols)}, %(src)s, 'vlm', %(emb)s, %(cover)s,
                            %(seen)s, %(seen)s, %(pv)s)
                    ON CONFLICT (post_id, segment_idx, item_idx) DO UPDATE SET
                      {", ".join(f"{c} = EXCLUDED.{c}" for c in cols)}, attr_sources = EXCLUDED.attr_sources,
                      extraction = 'vlm', prompt_version = EXCLUDED.prompt_version
                    RETURNING id""",
                {**row, "post_id": post.id, "seg": lst["segment_idx"], "k": k, "source": post.source,
                 "owner": post.owner_session, "sender": post.sender_ref, "src": Jsonb(src), "emb": lst["embedding"],
                 "cover": lst["cover_image_id"], "seen": post.last_msg_at, "pv": PROMPT_VERSION}).fetchone()[0])
    return ids


# ---------- threads ----------

class Worker:
    def __init__(self, ctx: LocalCtx, vlm: VlmCtx | None = None):
        self.ctx = ctx
        self.vlm = vlm
        self._event = threading.Event()
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []

    def start(self) -> None:
        self._threads = [threading.Thread(target=self._local_loop, name="worker-local", daemon=True)]
        if not self.ctx.settings.demo_mode:
            self._threads.append(threading.Thread(target=self._sweep_loop, name="worker-sweeper", daemon=True))
        if self.vlm is not None:
            self._threads += [threading.Thread(target=self._vlm_loop, name=f"worker-vlm-{i}", daemon=True)
                              for i in range(max(1, self.vlm.settings.vlm_concurrency))]
        for t in self._threads:
            t.start()

    def wake(self) -> None:
        self._event.set()

    def stop(self, timeout: float = 10) -> None:
        self._stop.set()
        self._event.set()
        for t in self._threads:
            t.join(timeout)

    def _local_loop(self) -> None:
        s = self.ctx.settings
        while not self._stop.is_set():
            if not self.ctx.models.ready.is_set():
                self._stop.wait(1)
                continue
            self._event.clear()
            try:
                with db.tx(self.ctx.pool) as conn:
                    post = claim_local(conn, s.lease_local_s, s.max_local_attempts)
            except Exception:
                log.exception("worker claim failed")
                self._stop.wait(s.worker_poll_s or 5)
                continue
            if post is None:
                self._event.wait(s.worker_poll_s or None)
            else:
                process_local(self.ctx, post)

    def _vlm_loop(self) -> None:
        s, v = self.ctx.settings, self.vlm
        while not self._stop.is_set():
            self._event.clear()
            post = None
            try:
                if not v.breaker.is_open():
                    with db.tx(v.pool) as conn:
                        if budget_available(conn, s.vlm_daily_cap):
                            post = claim_vlm(conn, s.lease_vlm_s)
            except Exception:
                log.exception("vlm claim failed")
                self._stop.wait(s.worker_poll_s or 5)
                continue
            if post is None:
                # no claimable post, budget spent or breaker open: wait for a wake or the poll interval
                self._event.wait(s.worker_poll_s or 60)
            else:
                process_vlm(v, post)
                self._event.set()  # a finished post may have released work for another VLM thread

    def _sweep_loop(self) -> None:
        while not self._stop.wait(SWEEP_INTERVAL_S):
            try:
                sweep_once(self.ctx.pool, self.ctx.settings)
            except Exception:
                log.exception("sweep failed")


# ---------- CLI ----------

def drain(ctx: LocalCtx, limit: int | None = None, progress_every: int = 50) -> Counter:
    s = ctx.settings
    sweep_once(ctx.pool, s)
    done: Counter = Counter()
    t0 = time.monotonic()
    n = 0
    while limit is None or n < limit:
        with db.tx(ctx.pool) as conn:
            post = claim_local(conn, s.lease_local_s, s.max_local_attempts)
        if post is None:
            break
        done[process_local(ctx, post) or "lost_claim"] += 1
        n += 1
        if n % progress_every == 0:
            rate = n / (time.monotonic() - t0)
            print(f"drain {n} posts, {rate:.2f} posts/s, {dict(done)}", flush=True)
    return done


def drain_vlm(ctx: VlmCtx, limit: int | None = None, progress_every: int = 50) -> Counter:
    """VLM_CONCURRENCY threads claim and process until nothing is claimable, the budget is spent, the breaker
    opens, or `limit` posts were taken. Caps and the ledger apply exactly as in the server."""
    s = ctx.settings
    done: Counter = Counter()
    lock = threading.Lock()
    taken = [0]
    t0 = time.monotonic()

    def loop() -> None:
        while True:
            if ctx.breaker.is_open():
                return
            with lock:
                if limit is not None and taken[0] >= limit:
                    return
                taken[0] += 1
            with db.tx(ctx.pool) as conn:
                post = claim_vlm(conn, s.lease_vlm_s) if budget_available(conn, s.vlm_daily_cap) else None
            if post is None:
                return
            status = process_vlm(ctx, post) or "lost_claim"
            with lock:
                done[status] += 1
                n = sum(done.values())
            if n % progress_every == 0:
                print(f"drain --vlm {n} posts, {n / (time.monotonic() - t0):.2f} posts/s, {dict(done)}", flush=True)

    threads = [threading.Thread(target=loop, name=f"drain-vlm-{i}") for i in range(max(1, s.vlm_concurrency))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    if ctx.breaker.is_open():
        print("drain --vlm: breaker open (auth or billing error); stopped early", flush=True)
    return done


def make_vlm_ctx(pool: ConnectionPool, settings: Settings, store: MediaStore) -> VlmCtx | None:
    from app.pipeline.vlm import get_backend

    backend = get_backend(settings)
    if backend is None:
        return None
    return VlmCtx(pool, settings, store, backend, CircuitBreaker(), RateLimiter(settings.vlm_rpm))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m app.worker")
    ap.add_argument("--drain", action="store_true", required=True)
    ap.add_argument("--vlm", action="store_true", help="also drain the VLM stage (spends credits; caps apply)")
    ap.add_argument("--limit", type=int)
    args = ap.parse_args(argv)

    from app.main import configure_logging
    from app.media_store import LocalDirStore
    from app.pipeline.models import ModelRegistry

    configure_logging()
    settings = Settings()
    pool = db.make_pool(settings.database_url, max(2, settings.vlm_concurrency + 2) if args.vlm else 2)
    try:
        with db.tx(pool) as conn:
            role = conn.execute("SELECT value FROM db_meta WHERE key = 'role'").fetchone()[0]
        if role != ("demo" if settings.demo_mode else "local"):
            print(f"refusing: db_meta.role is {role!r}", file=sys.stderr)
            return 2
        if settings.demo_mode:
            print("refusing: the demo media store arrives in build step 13", file=sys.stderr)
            return 2
        with db.tx(pool) as conn:
            local_work = conn.execute("SELECT count(*) FROM posts WHERE status IN ('received', 'processing')"
                                      ).fetchone()[0]
        models = ModelRegistry(settings.models_dir, settings.demo_mode)
        if local_work:
            models.load()  # ~10–70 s; skipped when only the VLM stage has work
        ctx = LocalCtx(pool, settings, models, LocalDirStore(settings.media_dir))
        done = drain(ctx, args.limit) if local_work else Counter()
        if args.vlm:
            vctx = make_vlm_ctx(pool, settings, ctx.store)
            if vctx is None:
                print("VLM_PROVIDER is off; skipping the VLM stage", file=sys.stderr)
            else:
                done.update({f"vlm:{k}": v for k, v in drain_vlm(vctx, args.limit).items()})
        with db.tx(pool) as conn:
            by_status = conn.execute(
                "SELECT status, coalesce(outcome, '-'), count(*) FROM posts GROUP BY 1, 2 ORDER BY 1, 2").fetchall()
        print(f"drained {sum(done.values())} posts: {dict(done)}")
        for st, oc, c in by_status:
            print(f"  {st:13} {oc:8} {c}")
    finally:
        pool.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
