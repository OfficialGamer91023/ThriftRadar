"""Worker local stage against the real Postgres with fake models. Spec: DESIGN.md §4.5; tests §6.3."""

import threading
import time
from datetime import timedelta
from decimal import Decimal

import psycopg
import pytest

from app import db
from app.ingest_service import MsgInput, PostInput, create_post
from app.media_store import LocalDirStore
from app.pipeline.caption import parse_caption
from app.settings import Settings
from app.worker import (LocalCtx, LostClaim, Worker, caption_attrs, claim_local, drain, extend_lease,
                        process_local, sweep_exhausted, sweep_stale_claims)
from tests.fakes import T0, FakeDetector, FakeEmbedder, FakeModels, FakeOcr, make_post


@pytest.fixture(scope="module")
def pool(db_url):
    p = db.make_pool(db_url, 6)
    yield p
    p.close()


@pytest.fixture
def conn(db_url, pool):
    with psycopg.connect(db_url) as c:
        c.execute("TRUNCATE posts RESTART IDENTITY CASCADE")
        c.commit()
        yield c


@pytest.fixture
def store(tmp_path):
    return LocalDirStore(tmp_path / "media")


def settings(**kw) -> Settings:
    return Settings(ingest_token="t", sender_hmac_key="k", load_models=False, **kw)


def ctx_for(pool, store, models=None, **kw) -> LocalCtx:
    return LocalCtx(pool, settings(**kw), models or FakeModels(), store)


def claim(pool, lease_s=600, max_attempts=3):
    with db.tx(pool) as c:
        return claim_local(c, lease_s, max_attempts)


def one(conn, sql, *args):
    return conn.execute(sql, args).fetchone()


COMPLETE = "Nike AF1 size 42 Rs 4500"


# ---------- claims and leases ----------

def test_claim_priority_then_age(conn, pool, store):
    make_post(conn, store, "old-export", [1], source="chat_export")
    make_post(conn, store, "live", [2], source="whatsapp")
    first = claim(pool)
    assert first.source == "whatsapp" and first.attempts == 1
    assert claim(pool).source == "chat_export"
    assert claim(pool) is None


def test_concurrent_claims_never_share(conn, pool, store):
    for i in range(6):
        make_post(conn, store, f"p{i}", [10 + i])
    got, barrier = [], threading.Barrier(8)

    def grab():
        barrier.wait()
        p = claim(pool)
        got.append(p.id if p else None)

    threads = [threading.Thread(target=grab) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    ids = [g for g in got if g is not None]
    assert len(ids) == 6 and len(set(ids)) == 6 and got.count(None) == 2


def test_extend_lease_wrong_token_and_reclaimed(conn, pool, store):
    make_post(conn, store, "p", [1])
    old = claim(pool, lease_s=600)
    extend_lease(pool, old, 600)  # ours: fine
    conn.execute("UPDATE posts SET lease_expires_at = now() - interval '1 s'")
    conn.commit()
    sweep_stale_claims(conn)
    conn.commit()
    new = claim(pool)
    assert new.id == old.id and new.claim_token != old.claim_token
    with pytest.raises(LostClaim):
        extend_lease(pool, old, 600)
    extend_lease(pool, new, 600)


# ---------- process_local ----------

def test_caption_complete_post_is_done_without_ocr(conn, pool, store):
    pid = make_post(conn, store, "p", [1, 2, 3], caption=COMPLETE)
    models = FakeModels()
    status = process_local(ctx_for(pool, store, models), claim(pool))
    assert status == "done" and models.ocr.calls == 0
    assert one(conn, "SELECT status, outcome, claim_token, last_error FROM posts") == ("done", "listed", None, None)
    l = one(conn, """SELECT brand, size_label, size_eu, price_amount, currency, attr_sources, extraction, vlm_missing,
                            cover_image_id = (SELECT id FROM images WHERE seq = 0), first_seen_at FROM listings""")
    assert l[:5] == ("Nike", "EU 42", Decimal("42.0"), 4500, "PKR")
    assert l[5] == {"brand": "caption", "size": "caption", "price": "caption"}
    assert l[6:] == ("local", None, True, T0 + timedelta(seconds=2))
    assert one(conn, "SELECT count(*) FROM images WHERE segment_idx = 0 AND embedding IS NOT NULL "
                     "AND primary_box IS NOT NULL AND detections IS NOT NULL")[0] == 3
    assert one(conn, "SELECT match_kind, post_id FROM listing_sightings") == ("origin", pid)


def test_trailing_text_is_the_caption_for_exports(conn, pool, store):
    make_post(conn, store, "p", [1, 2], text="Adidas samba\nsize 43\nprice 6000")
    process_local(ctx_for(pool, store), claim(pool))
    assert one(conn, "SELECT brand, size_label, price_amount FROM listings") == ("Adidas", "EU 43", 6000)


def test_missing_size_runs_ocr_and_stops_at_first_hit(conn, pool, store):
    make_post(conn, store, "p", [1, 2, 3], caption="Nike Rs 4500")
    ocr = FakeOcr([[("EUR 42 UK 8", 0.95)], [("EUR 44", 0.95)]])
    status = process_local(ctx_for(pool, store, FakeModels(ocr=ocr)), claim(pool))
    assert status == "done" and ocr.calls == 1
    l = one(conn, "SELECT size_label, size_eu, attr_sources->>'size' FROM listings")
    assert l == ("EU 42", Decimal("42.0"), "ocr")
    assert one(conn, "SELECT count(*) FROM images WHERE ocr_ran")[0] == 1


def test_missing_fields_go_to_vlm_unless_policy_never(conn, pool, store):
    make_post(conn, store, "auto", [1], caption="Nike size 42")
    make_post(conn, store, "never", [2], caption="Nike size 42", vlm_policy="never")
    c = ctx_for(pool, store)
    assert process_local(c, claim(pool)) == "awaiting_vlm"
    assert process_local(c, claim(pool)) == "done"
    rows = conn.execute("SELECT p.idempotency_key, l.vlm_missing FROM listings l JOIN posts p ON p.id = l.post_id "
                        "ORDER BY 1").fetchall()
    assert rows == [("auto", ["price"]), ("never", None)]


def test_multi_item_goes_to_vlm(conn, pool, store):
    make_post(conn, store, "p", [1, 2], caption=COMPLETE)
    status = process_local(ctx_for(pool, store, FakeModels(detector=FakeDetector(boxes=3))), claim(pool))
    assert status == "awaiting_vlm"
    assert one(conn, "SELECT vlm_missing FROM listings")[0] == []  # nothing missing, but multi_item: VLM still wanted


def test_no_shoe_makes_no_listing(conn, pool, store):
    make_post(conn, store, "p", [1, 2], caption=COMPLETE)
    status = process_local(ctx_for(pool, store, FakeModels(detector=FakeDetector(boxes=0))), claim(pool))
    assert status == "done"
    assert one(conn, "SELECT outcome FROM posts")[0] == "no_shoe"
    assert one(conn, "SELECT count(*) FROM listings")[0] == 0


def test_phash_repost_skips_models_and_updates_listing(conn, pool, store):
    make_post(conn, store, "first", [1, 2, 3])  # no text at all
    models = FakeModels()
    c = ctx_for(pool, store, models)
    process_local(c, claim(pool))
    det_calls = models.detector.calls
    make_post(conn, store, "again", [1, 2, 3], text="Nike size 42 now 4000", at=T0 + timedelta(days=10))
    assert process_local(c, claim(pool)) == "done"
    assert models.detector.calls == det_calls  # no model ran for the repost
    assert one(conn, "SELECT outcome FROM posts WHERE idempotency_key = 'again'")[0] == "repost"
    l = one(conn, "SELECT repost_count, price_amount, brand, size_label, attr_sources, last_seen_at, first_seen_at "
                  "FROM listings")
    assert l[:4] == (1, 4000, "Nike", "EU 42")
    assert l[4] == {"brand": "caption", "size": "caption", "price": "caption"}
    assert l[5] == T0 + timedelta(days=10, seconds=3) and l[6] == T0 + timedelta(seconds=2)
    assert one(conn, "SELECT count(*) FROM listings")[0] == 1
    kinds = [r[0] for r in conn.execute("SELECT match_kind FROM listing_sightings ORDER BY id")]
    assert kinds == ["origin", "phash"]
    # the repost's images point at its segment, so a third post chains to the same listing
    make_post(conn, store, "third", [1, 2, 3], at=T0 + timedelta(days=20))
    process_local(c, claim(pool))
    assert one(conn, "SELECT count(*), max(repost_count) FROM listings") == (1, 2)


def test_backfill_window_is_relative_to_the_post_not_now(conn, pool, store):
    long_ago = T0 - timedelta(days=200)
    make_post(conn, store, "a", [1, 2], at=long_ago)
    make_post(conn, store, "b", [1, 2], at=long_ago + timedelta(days=30))
    make_post(conn, store, "c", [1, 2], at=long_ago + timedelta(days=150))  # 120 days after b: outside 90
    c = ctx_for(pool, store)
    for _ in range(3):
        process_local(c, claim(pool))
    outcomes = [r[0] for r in conn.execute("SELECT outcome FROM posts ORDER BY id")]
    assert outcomes == ["listed", "repost", "listed"]


def test_lost_claim_before_commit_rolls_back(conn, pool, store, db_url):
    make_post(conn, store, "p", [1, 2], caption=COMPLETE)
    stolen = {}

    def steal():  # another worker takes the post after our lease expired
        with psycopg.connect(db_url) as c2:
            c2.execute("UPDATE posts SET lease_expires_at = now() - interval '1 s'")
            sweep_stale_claims(c2)
            c2.commit()
        stolen["post"] = claim(pool)

    models = FakeModels(embedder=FakeEmbedder(on_call=steal))  # after the last extend_lease before commit
    assert process_local(ctx_for(pool, store, models), claim(pool)) is None
    assert one(conn, "SELECT count(*) FROM listings")[0] == 0
    assert one(conn, "SELECT count(*) FROM images WHERE segment_idx IS NOT NULL")[0] == 0
    assert one(conn, "SELECT status, claim_token FROM posts") == ("processing", stolen["post"].claim_token)
    assert process_local(ctx_for(pool, store), stolen["post"]) == "done"
    assert one(conn, "SELECT count(*) FROM listings")[0] == 1


def test_error_backs_off_then_rerun_gives_one_listing(conn, pool, store):
    make_post(conn, store, "p", [1, 2], caption=COMPLETE)
    models = FakeModels(embedder=FakeEmbedder(fail_times=1))
    c = ctx_for(pool, store, models)
    assert process_local(c, claim(pool)) == "received"
    row = one(conn, "SELECT status, last_error, claim_token, next_attempt_at > now(), attempts FROM posts")
    assert row == ("received", "RuntimeError", None, True, 1)
    assert claim(pool) is None  # backing off
    conn.execute("UPDATE posts SET next_attempt_at = now()")
    conn.commit()
    assert process_local(c, claim(pool)) == "done"
    assert one(conn, "SELECT count(*) FROM listings")[0] == 1
    assert one(conn, "SELECT count(*) FROM listing_sightings")[0] == 1


def test_media_missing_everywhere_fails(conn, pool, store):
    make_post(conn, None, "p", [1, 2], caption=COMPLETE)  # images never written to the store
    assert process_local(ctx_for(pool, store), claim(pool)) == "failed"
    assert one(conn, "SELECT status, last_error FROM posts") == ("failed", "media_missing")


def test_post_with_no_images_fails(conn, pool, store):
    msgs = [MsgInput("m1", "image", T0, "Nike", None, reject_reason="corrupt"),
            MsgInput("m2", "text", T0, "size 42")]
    create_post(conn, PostInput("chat_export", "k", "c" * 32, "a" * 32, msgs))
    conn.commit()
    assert process_local(ctx_for(pool, store), claim(pool)) == "failed"
    assert one(conn, "SELECT last_error FROM posts")[0] == "no_images"


def test_seed_post_uses_ground_truth_and_skips_ocr(conn, pool, store):
    attrs = {"brand": "Vans", "model": "Old Skool", "colour": "black", "size_label": "UK 8", "size_eu": 42,
             "price": 3000, "currency": "PKR", "condition": "used"}
    make_post(conn, store, "seed:x", [1], source="demo_seed", vlm_policy="never", seed_attrs=attrs)
    models = FakeModels(detector=FakeDetector(boxes=0))  # seeds are exempt from no_shoe
    assert process_local(ctx_for(pool, store, models), claim(pool)) == "done"
    l = one(conn, "SELECT brand, model, size_label, price_amount, extraction, attr_sources->>'brand' FROM listings")
    assert l == ("Vans", "Old Skool", "UK 8", 3000, "seed", "seed") and models.ocr.calls == 0


def test_siglip_brand_when_confident(conn, pool, store):
    import numpy as np

    from tests.fakes import unit

    make_post(conn, store, "p", [1], caption="size 42 Rs 4000")
    # brand_text rows: the image's own embedding (cos 1) and an unrelated vector
    models = FakeModels()
    probe = models.embedder.embed_images
    captured = {}

    def embed(imgs):
        out = probe(imgs)
        captured["v"] = out[0]
        models.brand_text = np.stack([unit(5), out[0]])
        return out

    models.embedder.embed_images = embed
    models.brand_names = ["Puma", "Asics"]
    process_local(ctx_for(pool, store, models, siglip_brand_min_cos=0.5), claim(pool))
    assert one(conn, "SELECT brand, attr_sources->>'brand' FROM listings") == ("Asics", "siglip")


# ---------- caption merging (pure) ----------

def test_caption_groups_fall_back_to_extra_text_per_group():
    attrs, sources, amb = caption_attrs(parse_caption("Nike size 42"), parse_caption("Adidas 43 Rs 5000"), "")
    assert (attrs["brand"], attrs["size_label"], attrs["price_amount"]) == ("Nike", "EU 42", 5000)
    assert sources == {"brand": "caption", "size": "caption", "price": "caption"} and not amb
    attrs, _, amb = caption_attrs(parse_caption("size 40-42"), parse_caption("size 44"), "")
    assert attrs["size_label"] is None and amb  # an ambiguous caption is not overridden by extra text
    attrs, _, _ = caption_attrs(parse_caption("4500/-"), parse_caption(""), "PKR")
    assert (attrs["price_amount"], attrs["currency"]) == (4500, "PKR")  # DEFAULT_CURRENCY fills in
    attrs, _, _ = caption_attrs(parse_caption("Nike"), parse_caption(""), "PKR")
    assert attrs["currency"] is None  # ...but only when there is a price


# ---------- sweeper ----------

def test_sweepers(conn, pool, store):
    for i in range(3):
        make_post(conn, store, f"p{i}", [i + 1])
    conn.execute("""UPDATE posts SET status = 'processing', stage = 'local', claim_token = gen_random_uuid(),
                           lease_expires_at = now() - interval '1 s' WHERE idempotency_key = 'p0'""")
    conn.execute("""UPDATE posts SET status = 'processing', stage = 'vlm', claim_token = gen_random_uuid(),
                           lease_expires_at = now() - interval '1 s' WHERE idempotency_key = 'p1'""")
    conn.execute("UPDATE posts SET attempts = 3 WHERE idempotency_key = 'p2'")
    assert len(sweep_stale_claims(conn)) == 2
    assert sweep_exhausted(conn, 3) == [3]
    rows = conn.execute("SELECT idempotency_key, status, last_error FROM posts ORDER BY 1").fetchall()
    assert rows == [("p0", "received", "lease_expired"), ("p1", "awaiting_vlm", "lease_expired"),
                    ("p2", "failed", None)]


# ---------- threads and drain ----------

def test_worker_thread_processes_on_wake(conn, pool, store):
    w = Worker(ctx_for(pool, store, worker_poll_s=0))
    w.start()
    try:
        make_post(conn, store, "p", [1], caption=COMPLETE)
        w.wake()
        deadline = time.monotonic() + 10
        while one(conn, "SELECT status FROM posts")[0] != "done" and time.monotonic() < deadline:
            time.sleep(0.05)
            conn.rollback()
        assert one(conn, "SELECT status FROM posts")[0] == "done"
    finally:
        w.stop()
    assert not any(t.is_alive() for t in w._threads)


def test_drain_processes_everything(conn, pool, store):
    for i in range(4):
        make_post(conn, store, f"p{i}", [20 + i], caption=COMPLETE)
    done = drain(ctx_for(pool, store), progress_every=1000)
    assert done == {"done": 4}
    assert one(conn, "SELECT count(*) FROM listings")[0] == 4
