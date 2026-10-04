"""Repost detection and brand kNN. Spec: DESIGN.md §4.5 `find_repost_phash`, `find_repost_embedding`,
`apply_repost`, `knn_brand`."""

import math
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

import numpy as np
import psycopg
from psycopg.types.json import Jsonb

TRUSTED_BRAND_SOURCES = ("caption", "ocr", "vlm", "seed")

@dataclass(frozen=True)
class RepostHit:
    listing_id: int
    kind: Literal["phash", "embedding"]
    score: float  # images matched (phash) or cosine (embedding)


def find_repost_phash(conn: psycopg.Connection, *, post_id: int, sender_ref: str, ref_time: datetime,
                      phashes: list[int], window_days: int, max_dist: int, xseller_max_dist: int,
                      owner_session: str | None = None) -> RepostHit | None:
    """`owner_session` (demo uploads): a post may only merge into listings of the same session, and a post without
    one never merges into an upload. Otherwise a visitor could rewrite a seed listing or another visitor's."""
    if not phashes:
        return None
    row = conn.execute(
        """SELECT s.listing_id, count(DISTINCT i_new.seq) AS matched
           FROM unnest(%(ph)s::bigint[]) WITH ORDINALITY AS i_new(phash, seq)
           JOIN images i ON i.post_id <> %(post)s
           JOIN listing_sightings s ON s.post_id = i.post_id AND s.segment_idx = i.segment_idx
           JOIN listings l ON l.id = s.listing_id
                          AND l.last_seen_at > %(ref)s - make_interval(days => %(window)s)
                          AND l.owner_session IS NOT DISTINCT FROM %(owner)s
           WHERE bit_count((i.phash # i_new.phash)::bit(64)) <=
                 CASE WHEN l.sender_ref = %(sender)s THEN %(max)s ELSE %(xmax)s END
           GROUP BY s.listing_id
           ORDER BY matched DESC, s.listing_id
           LIMIT 1""",
        {"ph": phashes, "post": post_id, "ref": ref_time, "window": window_days, "sender": sender_ref,
         "max": max_dist, "xmax": xseller_max_dist, "owner": owner_session},
    ).fetchone()
    if row is None or row[1] < math.ceil(len(phashes) / 2):
        return None
    return RepostHit(row[0], "phash", float(row[1]))


def find_repost_embedding(conn: psycopg.Connection, *, post_id: int, sender_ref: str, ref_time: datetime,
                          emb: np.ndarray, window_days: int, min_cos: float) -> RepostHit | None:
    row = conn.execute(
        """WITH cand AS MATERIALIZED (
             SELECT id, embedding FROM listings
             WHERE sender_ref = %(sender)s AND post_id <> %(post)s
               AND last_seen_at > %(ref)s - make_interval(days => %(window)s))
           SELECT id, 1 - (embedding <=> %(emb)s) AS cos FROM cand ORDER BY embedding <=> %(emb)s LIMIT 1""",
        {"sender": sender_ref, "post": post_id, "ref": ref_time, "window": window_days, "emb": emb},
    ).fetchone()
    if row is None or row[1] < min_cos:
        return None
    return RepostHit(row[0], "embedding", float(row[1]))


def apply_repost(conn: psycopg.Connection, hit: RepostHit, *, post_id: int, segment_idx: int, seen_at: datetime,
                 attrs: dict, sources: dict) -> bool:
    """Record a sighting; only a new sighting bumps the listing. Returns True if it was new."""
    new = conn.execute(
        """INSERT INTO listing_sightings (listing_id, post_id, segment_idx, seen_at, price_amount, match_kind)
           VALUES (%s, %s, %s, %s, %s, %s)
           ON CONFLICT (post_id, segment_idx) DO NOTHING RETURNING id""",
        (hit.listing_id, post_id, segment_idx, seen_at, attrs.get("price_amount"), hit.kind),
    ).fetchone()
    if new is None:
        return False

    cur = conn.execute(
        "SELECT brand, size_label, size_eu, size_approx, currency, price_on_request, attr_sources "
        "FROM listings WHERE id = %s FOR UPDATE", (hit.listing_id,)).fetchone()
    current = dict(zip(("brand", "size_label", "size_eu", "size_approx", "currency", "price_on_request"), cur[:6]))
    attr_sources = dict(cur[6])
    fill: dict = {}
    if current["brand"] is None and attrs.get("brand"):
        fill["brand"] = attrs["brand"]
        attr_sources["brand"] = sources.get("brand", "caption")
    if current["size_label"] is None and attrs.get("size_label"):
        for f in ("size_label", "size_eu", "size_approx"):
            fill[f] = attrs.get(f)
        attr_sources["size"] = sources.get("size", "caption")
    if current["currency"] is None and attrs.get("currency"):
        fill["currency"] = attrs["currency"]
    if not current["price_on_request"] and attrs.get("price_on_request"):
        fill["price_on_request"] = True
        attr_sources.setdefault("price", sources.get("price", "caption"))
    if attrs.get("price_amount") is not None:
        attr_sources["price"] = sources.get("price", "caption")

    sets = "".join(f", {f} = %({f})s" for f in fill)
    conn.execute(
        f"""UPDATE listings SET last_seen_at = greatest(last_seen_at, %(seen)s),
                               first_seen_at = least(first_seen_at, %(seen)s),
                               repost_count = repost_count + 1,
                               price_amount = COALESCE(%(price)s, price_amount),
                               attr_sources = %(src)s{sets}
            WHERE id = %(id)s""",
        {"seen": seen_at, "price": attrs.get("price_amount"), "src": Jsonb(attr_sources), "id": hit.listing_id,
         **fill},
    )
    return True


def knn_brand(conn: psycopg.Connection, post_id: int, emb: np.ndarray, k: int = 5,
              min_cos: float = 0.92, owner_session: str | None = None) -> tuple[str, float] | None:
    """Neighbours never include another demo session's uploads (their captions are that visitor's text)."""
    rows = conn.execute(
        """SELECT brand, 1 - (embedding <=> %(emb)s) FROM listings
           WHERE brand IS NOT NULL AND attr_sources->>'brand' = ANY(%(trusted)s) AND post_id <> %(post)s
             AND (owner_session IS NULL OR owner_session = %(owner)s)
           ORDER BY embedding <=> %(emb)s LIMIT %(k)s""",
        {"emb": emb, "trusted": list(TRUSTED_BRAND_SOURCES), "post": post_id, "k": k, "owner": owner_session},
    ).fetchall()
    if not rows or rows[0][1] < min_cos:
        return None
    counts: dict[str, int] = {}
    for brand, _ in rows:
        counts[brand] = counts.get(brand, 0) + 1
    brand, n = max(counts.items(), key=lambda kv: kv[1])
    return (brand, float(rows[0][1])) if n >= 3 else None
