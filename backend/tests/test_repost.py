"""Repost guards and kNN brand against the real Postgres. Spec: DESIGN.md §4.5; tests §6.3."""

from datetime import timedelta

import numpy as np
import psycopg
import pytest
from psycopg.types.json import Jsonb

from app.ingest_service import ImageInput, MsgInput, PostInput, create_post
from app.pipeline.repost import RepostHit, apply_repost, find_repost_embedding, find_repost_phash, knn_brand
from tests.fakes import T0, near, unit

SELLER, OTHER = "a" * 32, "b" * 32
BASE = 0x00000000FFFFFFFF  # 32 ones: pHash values are balanced


def flip(h: int, n: int) -> int:
    """Move n/2 ones into zero positions: distance n, still balanced. Returns a signed int64."""
    for k in range(n // 2):
        h ^= (1 << k) | (1 << (32 + k))
    return h - (1 << 64) if h >= (1 << 63) else h


@pytest.fixture
def conn(db_url):
    with psycopg.connect(db_url) as c:
        from pgvector.psycopg import register_vector
        register_vector(c)
        c.execute("TRUNCATE posts RESTART IDENTITY CASCADE")
        c.commit()
        yield c


def listed_post(conn, key, phashes, *, sender=SELLER, at=T0, emb=None, brand=None, brand_src="caption") -> int:
    """A processed post: images with segment 0, one listing, an origin sighting. Returns the listing id."""
    msgs = [MsgInput(f"{key}:{i}", "image", at, None,
                     ImageInput(bytes([i + 1]) * 31 + key.encode()[:1], ph, 10, 10, 1))
            for i, ph in enumerate(phashes)]
    pid = create_post(conn, PostInput("chat_export", key, "c" * 32, sender, msgs)).post_id
    conn.execute("UPDATE images SET segment_idx = 0 WHERE post_id = %s", (pid,))
    lid = conn.execute(
        """INSERT INTO listings (post_id, segment_idx, item_idx, source, sender_ref, brand, attr_sources, extraction,
                                 embedding, first_seen_at, last_seen_at)
           VALUES (%s, 0, 0, 'chat_export', %s, %s, %s, 'local', %s, %s, %s) RETURNING id""",
        (pid, sender, brand, Jsonb({"brand": brand_src} if brand else {}),
         emb if emb is not None else unit(hash(key) % 1000), at, at)).fetchone()[0]
    conn.execute("""INSERT INTO listing_sightings (listing_id, post_id, segment_idx, seen_at, match_kind)
                    VALUES (%s, %s, 0, %s, 'origin')""", (lid, pid, at))
    conn.commit()
    return lid


def phash_hit(conn, phashes, *, sender=SELLER, post_id=0, at=T0 + timedelta(days=1)):
    return find_repost_phash(conn, post_id=post_id, sender_ref=sender, ref_time=at, phashes=phashes,
                             window_days=90, max_dist=6, xseller_max_dist=2)


def test_phash_same_seller_within_threshold(conn):
    lid = listed_post(conn, "a", [flip(BASE, 0), flip(BASE, 8)])
    assert phash_hit(conn, [flip(BASE, 6)]) == RepostHit(lid, "phash", 1.0)


def test_phash_distances_and_sellers(conn):
    lid = listed_post(conn, "a", [BASE])
    for dist, sender, expect in [(6, SELLER, True), (8, SELLER, False), (6, OTHER, False), (2, OTHER, True)]:
        hit = phash_hit(conn, [flip(BASE, dist)], sender=sender)
        assert (hit is not None and hit.listing_id == lid) is expect, (dist, sender)


def test_phash_needs_half_the_segment(conn):
    listed_post(conn, "a", [BASE])
    unrelated = flip(0x0F0F0F0F0F0F0F0F, 0)
    assert phash_hit(conn, [BASE, unrelated]) is not None  # 1 of 2
    assert phash_hit(conn, [BASE, unrelated, unrelated ^ 0x3]) is None  # 1 of 3


def test_phash_window_and_own_post(conn):
    listed_post(conn, "a", [BASE], at=T0)
    assert phash_hit(conn, [BASE], at=T0 + timedelta(days=89)) is not None
    assert phash_hit(conn, [BASE], at=T0 + timedelta(days=91)) is None
    own = conn.execute("SELECT id FROM posts").fetchone()[0]
    assert phash_hit(conn, [BASE], post_id=own) is None


def test_embedding_same_seller_only(conn):
    v = unit(1)
    lid = listed_post(conn, "a", [BASE], emb=v)
    kw = dict(post_id=0, ref_time=T0, window_days=90, min_cos=0.95)
    hit = find_repost_embedding(conn, sender_ref=SELLER, emb=near(v, 0.96), **kw)
    assert hit.listing_id == lid and hit.kind == "embedding" and hit.score == pytest.approx(0.96, abs=1e-3)
    assert find_repost_embedding(conn, sender_ref=SELLER, emb=near(v, 0.90), **kw) is None
    assert find_repost_embedding(conn, sender_ref=OTHER, emb=near(v, 0.99), **kw) is None


def test_apply_repost_is_idempotent_and_fills_gaps(conn):
    lid = listed_post(conn, "a", [BASE], brand="Nike")
    pid = create_post(conn, PostInput("chat_export", "b", "c" * 32, SELLER, [
        MsgInput("b:0", "image", T0, None, ImageInput(b"\x09" * 32, BASE, 10, 10, 1))])).post_id
    attrs = {"brand": "Adidas", "size_label": "EU 42", "size_eu": 42, "size_approx": False,
             "price_amount": 4000, "currency": "PKR", "price_on_request": False}
    sources = {"brand": "caption", "size": "caption", "price": "caption"}
    seen = T0 + timedelta(days=3)
    hit = RepostHit(lid, "phash", 1)
    assert apply_repost(conn, hit, post_id=pid, segment_idx=0, seen_at=seen, attrs=attrs, sources=sources)
    assert not apply_repost(conn, hit, post_id=pid, segment_idx=0, seen_at=seen, attrs=attrs, sources=sources)
    row = conn.execute("SELECT repost_count, brand, size_label, price_amount, currency, last_seen_at, attr_sources "
                       "FROM listings").fetchone()
    assert row[:6] == (1, "Nike", "EU 42", 4000, "PKR", seen)  # brand kept, gaps filled
    assert row[6] == {"brand": "caption", "size": "caption", "price": "caption"}
    assert conn.execute("SELECT price_amount FROM listing_sightings WHERE post_id = %s", (pid,)).fetchone() == (4000,)


def test_knn_brand_majority_of_trusted_labels(conn):
    v = unit(7)
    for i, (brand, src) in enumerate([("Nike", "caption"), ("Nike", "ocr"), ("Nike", "vlm"), ("Puma", "caption"),
                                      ("Adidas", "siglip"), ("Adidas", "siglip"), ("Adidas", "siglip")]):
        emb = near(v, 0.99 - 0.001 * i, seed=100 + i)
        listed_post(conn, f"k{i}", [flip(BASE, 0) ^ (i << 40)], emb=emb, brand=brand, brand_src=src)
    got = knn_brand(conn, 0, v)
    assert got[0] == "Nike" and got[1] == pytest.approx(0.99, abs=1e-3)
    assert knn_brand(conn, 0, v, min_cos=0.995) is None


def test_knn_brand_needs_three_agreeing(conn):
    v = unit(8)
    for i, brand in enumerate(["Nike", "Nike", "Puma", "Vans", "Asics"]):
        listed_post(conn, f"k{i}", [BASE ^ (i << 40)], emb=near(v, 0.97, seed=200 + i), brand=brand)
    assert knn_brand(conn, 0, np.asarray(v)) is None
