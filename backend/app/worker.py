"""The in-process worker: claims, leases, the local stage and the sweeper. Spec: DESIGN.md §4.5.

CLI: python -m app.worker --drain [--limit N]   (local stage only, in the foreground)
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
from datetime import datetime

import numpy as np
import psycopg
from PIL import Image
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from app import db
from app.media_store import MediaMissing, MediaStore
from app.pipeline.caption import CaptionFields, parse_caption, to_size_eu
from app.pipeline.decide import Decision, SegFlags, needs_vlm
from app.pipeline.detect import Det, primary_box
from app.pipeline.embed import segment_embedding
from app.pipeline.ocr import MIN_CONF, parse_size_tag
from app.pipeline.repost import RepostHit, apply_repost, find_repost_embedding, find_repost_phash, knn_brand
from app.pipeline.segment import Segment, SegMsg, segment_post
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
        "vlm_missing": (plan.decision.missing or None) if plan.decision.needed else None,
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


# ---------- threads ----------

class Worker:
    def __init__(self, ctx: LocalCtx):
        self.ctx = ctx
        self._event = threading.Event()
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []

    def start(self) -> None:
        self._threads = [threading.Thread(target=self._local_loop, name="worker-local", daemon=True)]
        if not self.ctx.settings.demo_mode:
            self._threads.append(threading.Thread(target=self._sweep_loop, name="worker-sweeper", daemon=True))
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


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m app.worker")
    ap.add_argument("--drain", action="store_true", required=True)
    ap.add_argument("--limit", type=int)
    args = ap.parse_args(argv)

    from app.main import configure_logging
    from app.media_store import LocalDirStore
    from app.pipeline.models import ModelRegistry

    configure_logging()
    settings = Settings()
    pool = db.make_pool(settings.database_url, 2)
    try:
        with db.tx(pool) as conn:
            role = conn.execute("SELECT value FROM db_meta WHERE key = 'role'").fetchone()[0]
        if role != ("demo" if settings.demo_mode else "local"):
            print(f"refusing: db_meta.role is {role!r}", file=sys.stderr)
            return 2
        if settings.demo_mode:
            print("refusing: the demo media store arrives in build step 13", file=sys.stderr)
            return 2
        models = ModelRegistry(settings.models_dir, settings.demo_mode)
        models.load()
        ctx = LocalCtx(pool, settings, models, LocalDirStore(settings.media_dir))
        done = drain(ctx, args.limit)
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
