"""process_vlm against the real Postgres with a fake backend. Spec: DESIGN.md §4.5 `process_vlm`; tests §6.3."""

from datetime import datetime, timedelta, timezone

import psycopg
import pytest

from app import db
from app.media_store import LocalDirStore
from app.pipeline.budget import CircuitBreaker, RateLimiter
from app.pipeline.vlm import PROMPT_VERSION, VlmResult
from app.settings import Settings
from app.worker import LocalCtx, VlmCtx, claim_local, claim_vlm, process_local, process_vlm, sweep_stale_claims
from scripts.requeue import requeue_vlm
from tests.fakes import FakeDetector, FakeModels, FakeVlmBackend, make_post, vlm_doc


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


def settings(**kw):
    kw.setdefault("vlm_provider", "ollama")  # anything but "off", so the local stage asks for the VLM
    return Settings(ingest_token="t", sender_hmac_key="k", load_models=False, **kw)


def awaiting(conn, pool, store, caption="Nike size 42", models=None, key="p", vlm_policy="auto"):
    """A post that went through the local stage and now waits for the VLM."""
    make_post(conn, store, key, [1, 2], caption=caption, vlm_policy=vlm_policy)
    with db.tx(pool) as c:
        post = claim_local(c, 600, 3)
    status = process_local(LocalCtx(pool, settings(), models or FakeModels(), store), post)
    assert status == "awaiting_vlm"


def vctx(pool, store, backend, **kw):
    return VlmCtx(pool, settings(**kw), store, backend, CircuitBreaker(), RateLimiter(10_000))


def claim(pool):
    with db.tx(pool) as c:
        return claim_vlm(c, 180)


def one(conn, sql, *args):
    conn.rollback()
    return conn.execute(sql, args).fetchone()


def test_ok_merges_and_finishes(conn, pool, store):
    awaiting(conn, pool, store)
    fake = FakeVlmBackend(vlm_doc({"brand": "Adidas", "model": "AF1", "colour": "white",
                                   "price": {"amount": 4000, "currency": "pkr"}}))
    assert process_vlm(vctx(pool, store, fake), claim(pool)) == "done"
    row = one(conn, "SELECT brand, model, colour, price_amount, currency, extraction, vlm_missing, attr_sources, "
                    "prompt_version FROM listings")
    assert row[:7] == ("Nike", "AF1", "white", 4000, "PKR", "vlm", None)  # caption brand kept
    assert row[7]["brand"] == "caption" and row[7]["price"] == "vlm" and row[8] == PROMPT_VERSION
    assert one(conn, "SELECT status, claim_token FROM posts") == ("done", None)
    call = one(conn, "SELECT status, attempt, response IS NOT NULL, cost_usd > 0 FROM vlm_calls")
    assert call == ("ok", 1, True, False)  # fake prices are 0
    text = fake.requests[0]["messages"][1]["content"][0]["text"]
    assert "Nike size 42" in text and "price" in text  # caption and the missing field are in the prompt


def test_cached_answer_is_never_paid_twice(conn, pool, store):
    awaiting(conn, pool, store)
    fake = FakeVlmBackend(vlm_doc({"price": {"amount": 4000, "currency": None}}))
    process_vlm(vctx(pool, store, fake), claim(pool))
    # simulate a crash after the call was recorded but before the listing commit
    conn.execute("UPDATE listings SET extraction = 'local', vlm_missing = '{price}', price_amount = NULL")
    conn.execute("UPDATE posts SET status = 'awaiting_vlm'")
    conn.commit()
    again = FakeVlmBackend()
    assert process_vlm(vctx(pool, store, again), claim(pool)) == "done"
    assert again.calls == 0 and one(conn, "SELECT price_amount FROM listings")[0] == 4000


def test_bad_json_repairs_once_then_fails(conn, pool, store):
    awaiting(conn, pool, store)
    fake = FakeVlmBackend("not json", '{"is_shoe_listing": true, "items": [}')
    assert process_vlm(vctx(pool, store, fake), claim(pool)) == "done"
    assert fake.calls == 2 and "not valid JSON" in fake.requests[1]["messages"][1]["content"][0]["text"]
    assert one(conn, "SELECT extraction, vlm_missing FROM listings") == ("vlm_failed", None)
    assert [r[0] for r in conn.execute("SELECT status FROM vlm_calls ORDER BY id")] == ["bad_json", "bad_json"]


def test_timeout_backs_off_and_counts(conn, pool, store):
    awaiting(conn, pool, store)
    fake = FakeVlmBackend(VlmResult("timeout", latency_ms=60000))
    assert process_vlm(vctx(pool, store, fake), claim(pool)) == "awaiting_vlm"
    row = one(conn, "SELECT status, vlm_attempts, last_error, next_attempt_at - now() FROM posts")
    assert row[:3] == ("awaiting_vlm", 1, "timeout:-") and timedelta(seconds=20) < row[3] <= timedelta(seconds=31)
    assert one(conn, "SELECT status FROM vlm_calls")[0] == "timeout"
    assert one(conn, "SELECT extraction FROM listings")[0] == "local"


def test_429_honours_retry_after_and_is_not_counted(conn, pool, store):
    awaiting(conn, pool, store)
    fake = FakeVlmBackend(VlmResult("http_error", 429, retry_after=300))
    process_vlm(vctx(pool, store, fake), claim(pool))
    delay = one(conn, "SELECT next_attempt_at - now() FROM posts")[0]
    assert timedelta(seconds=290) < delay <= timedelta(seconds=300)
    conn.execute("UPDATE posts SET next_attempt_at = now()")
    conn.commit()
    ok = FakeVlmBackend(vlm_doc({"price": {"amount": 1, "currency": None}}))
    process_vlm(vctx(pool, store, ok, vlm_max_attempts=1), claim(pool))  # the 429 didn't use the only attempt
    assert ok.calls == 1


def test_402_opens_breaker_and_refunds_the_attempt(conn, pool, store):
    awaiting(conn, pool, store)
    ctx = vctx(pool, store, FakeVlmBackend(VlmResult("http_error", 402)))
    assert process_vlm(ctx, claim(pool)) == "awaiting_vlm"
    assert ctx.breaker.is_open()
    row = one(conn, "SELECT status, vlm_attempts, last_error, next_attempt_at > now() + interval '10 min' FROM posts")
    assert row == ("awaiting_vlm", 0, "http_402", True)
    assert one(conn, "SELECT extraction, vlm_missing FROM listings") == ("local", ["price"])
    conn.execute("UPDATE posts SET next_attempt_at = now()")
    conn.commit()
    assert process_vlm(ctx, claim(pool)) == "awaiting_vlm" and ctx.backend.calls == 1  # breaker: no second call


def test_daily_cap_waits_until_midnight(conn, pool, store):
    awaiting(conn, pool, store)
    fake = FakeVlmBackend()
    assert process_vlm(vctx(pool, store, fake, vlm_daily_cap=0), claim(pool)) == "awaiting_vlm"
    row = one(conn, "SELECT vlm_attempts, last_error, next_attempt_at FROM posts")
    midnight = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
    assert fake.calls == 0 and row == (0, "daily_cap", midnight)


def test_post_cap_fails_the_segment(conn, pool, store):
    awaiting(conn, pool, store)
    fake = FakeVlmBackend(VlmResult("timeout"))
    process_vlm(vctx(pool, store, fake, vlm_max_attempts=1), claim(pool))
    conn.execute("UPDATE posts SET next_attempt_at = now()")
    conn.commit()
    assert process_vlm(vctx(pool, store, fake, vlm_max_attempts=1), claim(pool)) == "done"
    assert fake.calls == 1 and one(conn, "SELECT extraction FROM listings")[0] == "vlm_failed"


def test_multi_item_creates_extra_listings(conn, pool, store):
    awaiting(conn, pool, store, caption="Nike size 42 Rs 4000", models=FakeModels(detector=FakeDetector(boxes=3)))
    fake = FakeVlmBackend(vlm_doc({"brand": "Nike", "size": {"value": 42, "system": "EU"}},
                                  {"brand": "Puma", "size": {"value": 40, "system": "EU"}},
                                  {"brand": "Vans", "size": {"value": 44, "system": "EU"}}))
    process_vlm(vctx(pool, store, fake), claim(pool))
    rows = conn.execute("SELECT item_idx, brand, size_label, extraction FROM listings ORDER BY item_idx").fetchall()
    assert rows == [(0, "Nike", "EU 42", "vlm"), (1, "Puma", "EU 40", "vlm"), (2, "Vans", "EU 44", "vlm")]


def test_lost_lease_before_the_call_reserves_nothing(conn, pool, store, db_url):
    awaiting(conn, pool, store)
    old = claim(pool)
    with psycopg.connect(db_url) as c2:
        c2.execute("UPDATE posts SET lease_expires_at = now() - interval '1 s'")
        sweep_stale_claims(c2)
        c2.commit()
    claim(pool)  # someone else holds it now
    fake = FakeVlmBackend()
    assert process_vlm(vctx(pool, store, fake), old) is None
    assert fake.calls == 0 and one(conn, "SELECT count(*) FROM vlm_calls")[0] == 0


def test_not_a_shoe_keeps_local_attributes(conn, pool, store):
    awaiting(conn, pool, store)
    process_vlm(vctx(pool, store, FakeVlmBackend(vlm_doc(shoe=False))), claim(pool))
    assert one(conn, "SELECT brand, extraction FROM listings") == ("Nike", "vlm")


def test_size_missing_adds_unboxed_photos(conn, pool, store):
    boxes = lambda im: 1 if im.getpixel((0, 0)) == FIRST else 0  # noqa: E731
    from tests.imgutil import pattern
    global FIRST
    FIRST = pattern(1).getpixel((0, 0))
    awaiting(conn, pool, store, caption="Nike Rs 4000", models=FakeModels(detector=FakeDetector(boxes=boxes)))
    fake = FakeVlmBackend(vlm_doc({"size": {"value": 42, "system": "EU"}}))
    process_vlm(vctx(pool, store, fake), claim(pool))
    images = [p for p in fake.requests[0]["messages"][1]["content"] if p["type"] == "image_url"]
    assert len(images) == 2  # the boxed crop plus the unboxed photo
    assert one(conn, "SELECT size_label, attr_sources->>'size' FROM listings") == ("EU 42", "vlm")


def test_requeue_vlm_picks_only_listings_with_gaps(conn, pool, store):
    make_post(conn, store, "full", [1], caption="Nike size 42 Rs 4000", vlm_policy="never")
    make_post(conn, store, "gap", [2], caption="Nike size 42", vlm_policy="never")
    lctx = LocalCtx(pool, settings(), FakeModels(), store)
    for _ in range(2):
        with db.tx(pool) as c:
            post = claim_local(c, 600, 3)
        process_local(lctx, post)
    assert requeue_vlm(conn, "chat_export", None, "featherless", ["brand", "size", "price"], apply=False) == \
        {"posts": 1, "calls": 1}
    requeue_vlm(conn, "chat_export", None, "featherless", ["brand", "size", "price"], apply=True)
    conn.commit()
    rows = conn.execute("SELECT p.idempotency_key, p.status, p.vlm_policy, l.vlm_missing FROM posts p "
                        "JOIN listings l ON l.post_id = p.id ORDER BY 1").fetchall()
    assert rows == [("full", "done", "never", None), ("gap", "awaiting_vlm", "auto", ["price"])]


def test_drain_vlm_runs_threads_and_respects_the_daily_cap(conn, pool, store):
    from app.worker import drain_vlm

    for i in range(6):
        make_post(conn, store, f"p{i}", [10 + i], caption="Nike size 42")
        with db.tx(pool) as c:
            post = claim_local(c, 600, 3)
        process_local(LocalCtx(pool, settings(), FakeModels(), store), post)
    fake = FakeVlmBackend(*[vlm_doc({"price": {"amount": 100 + i, "currency": None}}) for i in range(6)])
    done = drain_vlm(vctx(pool, store, fake, vlm_concurrency=3, vlm_daily_cap=4))
    assert fake.calls == 4 and done["done"] == 4  # the cap holds under concurrency
    # threads that passed the cheap pre-check but lost the reservation release their post, unpaid
    assert set(done) <= {"done", "awaiting_vlm"}
    assert one(conn, "SELECT count(*) FROM vlm_calls")[0] == 4
    assert one(conn, "SELECT count(*) FROM posts WHERE status = 'awaiting_vlm'")[0] == 2
