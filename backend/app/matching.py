"""Wishlist matching. Spec: DESIGN.md §4.6 `match_listing`, `wishlist_filters`, `match_wishlist`."""

import re
from dataclasses import dataclass
from decimal import Decimal

import numpy as np
import psycopg

from app.pipeline.caption import normalize, parse_caption, to_size_eu

# Sub-brands match their parent: a Nike wishlist should see Jordans.
BRAND_FAMILY = {"jordan": "nike", "yeezy": "adidas"}
VLM_SIZE_SLACK = 1  # EU sizes; VLM-read sizes are softer evidence (6 of 8 right in the step 9 evaluation)
MAX_ACTIVE_WISHLISTS = 10
# Buyers write budgets, not prices: "under 5000", "below rs 4.5k", "max 6000", "budget 3k".
_BUDGET = re.compile(r"\b(?:under|below|max|maximum|upto|up to|budget|within|less than)\s*(?:rs\.?|pkr)?\s*"
                     r"(\d+(?:\.\d+)?)\s*(k)?\b")


def brand_family_sql(col: str) -> str:
    whens = " ".join(f"WHEN '{k}' THEN '{v}'" for k, v in BRAND_FAMILY.items())
    return f"(CASE lower({col}) {whens} ELSE lower({col}) END)"


def brand_family(brand: str | None) -> str | None:
    return BRAND_FAMILY.get(brand.lower(), brand.lower()) if brand else None


# The shared WHERE clause: filters, visibility, score. Unknown size or price passes (the UI says "size unknown").
MATCH_WHERE = f"""
    w.active
    AND (w.size_eu_min IS NULL OR l.size_eu IS NULL
         OR l.size_eu BETWEEN w.size_eu_min - (CASE WHEN l.attr_sources->>'size' = 'vlm' THEN {VLM_SIZE_SLACK} ELSE 0 END)
                          AND w.size_eu_max + (CASE WHEN l.attr_sources->>'size' = 'vlm' THEN {VLM_SIZE_SLACK} ELSE 0 END))
    AND (w.max_price IS NULL OR l.price_amount IS NULL OR l.price_amount <= w.max_price)
    AND (w.brand IS NULL OR {brand_family_sql('l.brand')} = {brand_family_sql('w.brand')})
    AND (w.owner = 'local' OR l.source = 'demo_seed' OR l.owner_session = w.owner)
    AND l.status = 'active'
    AND 1 - (w.query_embedding <=> l.embedding) >= w.min_score
"""


def match_listing(conn: psycopg.Connection, listing_id: int) -> list[int]:
    """New match ids for this listing across all active wishlists. Idempotent by the (wishlist, listing) key."""
    ids = [r[0] for r in conn.execute(
        f"""INSERT INTO matches (wishlist_id, listing_id, score)
            SELECT w.id, l.id, 1 - (w.query_embedding <=> l.embedding)
            FROM wishlists w JOIN listings l ON l.id = %s
            WHERE {MATCH_WHERE}
            ON CONFLICT (wishlist_id, listing_id) DO NOTHING RETURNING id""", (listing_id,)).fetchall()]
    conn.execute("UPDATE listings SET match_checked_at = now() WHERE id = %s", (listing_id,))
    return ids


def match_wishlist(conn: psycopg.Connection, wishlist_id: int) -> list[int]:
    """Every listing that qualifies right now, inserted as already notified: creating a wishlist never floods, and
    no old listing can notify later (e.g. when it is re-checked after a repost). An exact scan; fine at this size."""
    return [r[0] for r in conn.execute(
        f"""INSERT INTO matches (wishlist_id, listing_id, score, notified_at)
            SELECT w.id, l.id, 1 - (w.query_embedding <=> l.embedding), now()
            FROM wishlists w JOIN listings l ON true
            WHERE w.id = %s AND {MATCH_WHERE}
            ON CONFLICT (wishlist_id, listing_id) DO NOTHING RETURNING id""", (wishlist_id,)).fetchall()]


def wishlist_filters(text: str | None, *, size: float | None = None, max_price: int | None = None,
                     brand: str | None = None) -> dict:
    """Pure. Explicit values win; the rest comes from the text ("nike size 42 under 5000")."""
    f = parse_caption(text) if text else None
    if brand is None and f and f.brand:
        brand = f.brand
    size_min = size_max = None
    if size is not None:
        size_min = size_max = Decimal(str(size))
    elif f and f.size_value is not None and not f.size_ambiguous:
        eu, _ = to_size_eu(f.size_value, f.size_system or "EU")
        size_min = size_max = eu
    if max_price is None and text and (m := _BUDGET.search(normalize(text))):
        max_price = int(round(float(m.group(1)) * (1000 if m.group(2) else 1)))
    if max_price is None and f and f.price is not None and not f.price_ambiguous:
        max_price = f.price
    return {"brand": brand, "size_eu_min": size_min, "size_eu_max": size_max, "max_price": max_price}


@dataclass(frozen=True)
class NewWishlist:
    id: int
    matched: int
    filters: dict


def create_wishlist(conn: psycopg.Connection, *, owner: str, embedding: np.ndarray, min_score: float,
                    text: str | None = None, ref_image_sha: bytes | None = None, filters: dict) -> NewWishlist:
    """Insert, then match existing listings (suppressed notifications). The caller embeds the query."""
    active = conn.execute("SELECT count(*) FROM wishlists WHERE owner = %s AND active", (owner,)).fetchone()[0]
    if active >= MAX_ACTIVE_WISHLISTS:
        raise ValueError("too_many_wishlists")
    wid = conn.execute(
        """INSERT INTO wishlists (owner, query_text, ref_image_sha, query_embedding, size_eu_min, size_eu_max,
                                  max_price, brand, min_score)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id""",
        (owner, text, ref_image_sha, embedding, filters["size_eu_min"], filters["size_eu_max"],
         filters["max_price"], filters["brand"], min_score)).fetchone()[0]
    return NewWishlist(wid, len(match_wishlist(conn, wid)), filters)
