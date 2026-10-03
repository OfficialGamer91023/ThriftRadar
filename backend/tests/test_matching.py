"""Matching and notifications against the real Postgres. Spec: DESIGN.md §4.6; tests §6.3."""

from decimal import Decimal

import psycopg
import pytest
from pgvector.psycopg import register_vector
from psycopg.types.json import Jsonb

from app import db
from app.matching import MAX_ACTIVE_WISHLISTS, create_wishlist, match_listing, wishlist_filters
from app.media_store import LocalDirStore
from app.notify import MacNotifier, NullNotifier, describe, notify_match
from app.settings import Settings
from app.worker import LocalCtx, claim_local, process_local, retry_unnotified, sweep_unmatched
from tests.fakes import T0, FakeModels, make_post, near, unit

Q = unit(42)


@pytest.fixture
def conn(db_url):
    with psycopg.connect(db_url) as c:
        register_vector(c)
        c.execute("TRUNCATE posts, wishlists RESTART IDENTITY CASCADE")
        c.commit()
        yield c


@pytest.fixture(scope="module")
def pool(db_url):
    p = db.make_pool(db_url, 4)
    yield p
    p.close()


def listing(conn, key, *, cos=0.95, brand="Nike", size=None, size_src="caption", price=None, emb=None) -> int:
    pid = make_post(conn, None, key, [hash(key) % 1000])
    src = {"brand": "caption"}
    if size is not None:
        src["size"] = size_src
    lid = conn.execute(
        """INSERT INTO listings (post_id, segment_idx, item_idx, source, sender_ref, brand, size_label, size_eu,
                                 price_amount, attr_sources, extraction, embedding, first_seen_at, last_seen_at)
           VALUES (%s, 0, 0, 'chat_export', %s, %s, %s, %s, %s, %s, 'local', %s, %s, %s) RETURNING id""",
        (pid, "a" * 32, brand, f"EU {size}" if size else None, size, price, Jsonb(src),
         emb if emb is not None else near(Q, cos, seed=hash(key) % 997), T0, T0)).fetchone()[0]
    conn.commit()
    return lid


def wishlist(conn, text="white sneakers", min_score=0.5, **filters) -> int:
    f = {"brand": None, "size_eu_min": None, "size_eu_max": None, "max_price": None}
    f.update(filters)
    w = create_wishlist(conn, owner="local", embedding=Q, min_score=min_score, text=text, filters=f)
    conn.commit()
    return w.id


# ---------- match_listing ----------

def test_filters_and_score(conn):
    wishlist(conn, brand="Nike", size_eu_min=Decimal(42), size_eu_max=Decimal(42), max_price=5000)
    cases = {
        "ok": dict(size=42, price=4000), "unknown size and price pass": dict(),
        "too pricey": dict(size=42, price=6000), "wrong size": dict(size=44, price=4000),
        "other brand": dict(brand="Puma", size=42), "jordan is nike": dict(brand="Jordan", size=42),
        "low score": dict(cos=0.3, size=42), "vlm size within 1": dict(size=43, size_src="vlm"),
        "vlm size too far": dict(size=44, size_src="vlm"),
    }
    got = {name: bool(match_listing(conn, listing(conn, name.replace(" ", "-"), **kw)))
           for name, kw in cases.items()}
    assert got == {"ok": True, "unknown size and price pass": True, "too pricey": False, "wrong size": False,
                   "other brand": False, "jordan is nike": True, "low score": False, "vlm size within 1": True,
                   "vlm size too far": False}


def test_match_listing_is_idempotent_and_marks_checked(conn):
    wishlist(conn)
    lid = listing(conn, "a")
    assert len(match_listing(conn, lid)) == 1
    assert match_listing(conn, lid) == []
    assert conn.execute("SELECT match_checked_at IS NOT NULL FROM listings").fetchone()[0]


def test_new_wishlist_matches_old_listings_without_notifying(conn):
    for i in range(3):
        listing(conn, f"old{i}")
    wid = wishlist(conn)
    rows = conn.execute("SELECT count(*), count(notified_at) FROM matches WHERE wishlist_id = %s", (wid,)).fetchone()
    assert rows == (3, 3)


def test_wishlist_limit(conn):
    for i in range(MAX_ACTIVE_WISHLISTS):
        wishlist(conn, text=f"w{i}")
    with pytest.raises(ValueError):
        wishlist(conn, text="one too many")


def test_wishlist_filters_from_text():
    f = wishlist_filters("nike air force size 42 under 5000")
    assert (f["brand"], f["size_eu_min"], f["size_eu_max"], f["max_price"]) == ("Nike", Decimal(42), Decimal(42), 5000)
    f = wishlist_filters("white sneakers uk 8", brand="Adidas", max_price=3000)
    assert (f["brand"], f["size_eu_min"], f["max_price"]) == ("Adidas", Decimal("42.0"), 3000)
    f = wishlist_filters("size 40-42 shoes")
    assert f["size_eu_min"] is None  # a range is ambiguous: no size filter


# ---------- notifications ----------

def test_notify_once_and_retry_on_failure(conn):
    wishlist(conn)
    mid = match_listing(conn, listing(conn, "a", size=42, price=4500))[0]
    conn.commit()

    class Flaky(NullNotifier):
        def __init__(self):
            super().__init__()
            self.fail = True

        def send(self, title, body):
            self.sent.append((title, body))
            return not self.fail

    n = Flaky()
    assert not notify_match(conn, n, mid)
    assert conn.execute("SELECT notified_at, notify_attempts FROM matches").fetchone() == (None, 1)
    n.fail = False
    assert notify_match(conn, n, mid)
    assert not notify_match(conn, n, mid)  # already notified: guard
    assert len(n.sent) == 2 and n.sent[-1] == ("ThriftRadar: Nike", "Size EU 42 · Rs 4,500")


def test_describe_marks_ai_sizes_and_has_no_seller_fields():
    title, body = describe({"brand": "Adidas", "model": "Samba", "size_label": "UK 8", "size_src": "vlm",
                            "price_amount": 3000, "currency": "PKR", "repost_count": 2})
    assert title == "ThriftRadar: Adidas Samba" and body == "Size UK 8 (read by AI) · Rs 3,000 · posted 3×"


def test_mac_notifier_uses_argv_and_refuses_outside_local_macos():
    s = Settings(ingest_token="t", sender_hmac_key="k", notify_macos=True)
    calls = []
    MacNotifier(s, platform="darwin").send('Nike "x" & quit app', "a\nb" + "z" * 500,
                                            run=lambda argv, **kw: calls.append((argv, kw)) or type("R", (), {"returncode": 0})())
    argv, kw = calls[0]
    assert argv[0] == "/usr/bin/osascript" and argv[-2] == 'Nike "x" & quit app'  # data only in argv
    assert all('Nike' not in a for a in argv[:-2]) and len(argv[-1]) == 200 and "\n" not in argv[-1]
    assert kw["timeout"] == 5 and "shell" not in kw
    with pytest.raises(RuntimeError):
        MacNotifier(s, platform="linux")
    with pytest.raises(RuntimeError):
        MacNotifier(Settings(ingest_token="t", sender_hmac_key="k", DEMO_MODE="1", session_secret="x"),
                    platform="darwin")


# ---------- worker hooks and sweeps ----------

def test_new_listing_from_the_worker_notifies_once(conn, pool, tmp_path):
    store = LocalDirStore(tmp_path)
    models = FakeModels()
    # the wishlist's embedding is the embedding the fake embedder gives this post's crop
    make_post(conn, store, "p", [7], caption="Nike size 42 Rs 4000", vlm_policy="never")
    notifier = NullNotifier()
    ctx = LocalCtx(pool, Settings(ingest_token="t", sender_hmac_key="k"), models, store, notifier)
    seg_emb = None

    def capture(imgs, _orig=models.embedder.embed_images):
        nonlocal seg_emb
        out = _orig(imgs)
        seg_emb = out[0]
        create_wishlist(conn, owner="local", embedding=seg_emb, min_score=0.9, text="nike 42",
                        filters={"brand": "Nike", "size_eu_min": Decimal(42), "size_eu_max": Decimal(42),
                                 "max_price": 5000})
        conn.commit()
        return out

    models.embedder.embed_images = capture
    with db.tx(pool) as c:
        post = claim_local(c, 600, 3)
    assert process_local(ctx, post) == "done"
    assert len(notifier.sent) == 1 and notifier.sent[0][0] == "ThriftRadar: Nike"
    conn.rollback()
    assert conn.execute("SELECT count(*), count(notified_at) FROM matches").fetchone() == (1, 1)


def test_sweep_unmatched_and_retry_unnotified(conn, pool):
    wishlist(conn)
    lid = listing(conn, "a")
    conn.execute("UPDATE posts SET status = 'done'")
    conn.commit()
    n = NullNotifier()
    assert sweep_unmatched(pool, n) == 1 and len(n.sent) == 1
    assert sweep_unmatched(pool, n) == 0  # match_checked_at is set now
    # a match whose notification failed earlier is retried after 30 s
    conn.execute("UPDATE matches SET notified_at = NULL, created_at = now() - interval '1 minute'")
    conn.commit()
    assert retry_unnotified(pool, n) == 1 and len(n.sent) == 2
    assert lid


@pytest.mark.parametrize("text,price", [("under 5000", 5000), ("below rs 4.5k", 4500), ("budget 3k nike", 3000),
                                        ("max 6000", 6000), ("price 4000", 4000), ("white sneakers", None)])
def test_budget_phrases(text, price):
    assert wishlist_filters(text)["max_price"] == price
