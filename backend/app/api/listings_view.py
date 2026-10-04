"""Shared listing columns, JSON shape and visibility for the read APIs. Spec: DESIGN.md §4.7."""

LISTING_COLS = """l.id, l.post_id, l.item_idx, l.brand, l.model, l.colour, l.condition, l.gender, l.size_label, l.size_eu,
    l.size_approx, l.price_amount, l.currency, l.price_on_request, l.attr_sources, l.extraction, l.status,
    l.first_seen_at, l.last_seen_at, l.repost_count, l.source, ci.sha256 AS cover_sha"""
_NAMES = ("id", "post_id", "item_idx", "brand", "model", "colour", "condition", "gender", "size_label", "size_eu",
          "size_approx", "price_amount", "currency", "price_on_request", "attr_sources", "extraction", "status",
          "first_seen_at", "last_seen_at", "repost_count", "source", "cover_sha")


def visibility_sql(alias: str, owner: str | None) -> str:
    """Local mode sees everything. Demo (step 13): seed listings plus the session's own uploads."""
    if owner is None:
        return "true"
    return f"({alias}.source = 'demo_seed' OR {alias}.owner_session = %(owner)s)"


def media_url(sha: bytes | None) -> str | None:
    return f"/media/{bytes(sha).hex()}" if sha else None


def listing_json(row) -> dict:
    d = dict(zip(_NAMES, row[:len(_NAMES)]))
    src = d.pop("attr_sources") or {}
    out = {k: d[k] for k in ("id", "post_id", "item_idx", "brand", "model", "colour", "condition", "gender",
                             "size_label", "size_approx", "price_amount", "currency", "price_on_request",
                             "extraction", "status", "repost_count", "source")}
    out["size_eu"] = float(d["size_eu"]) if d["size_eu"] is not None else None
    out["sources"] = src  # e.g. {"size": "vlm"}: the UI says "size read by AI"
    out["first_seen_at"] = d["first_seen_at"].isoformat()
    out["last_seen_at"] = d["last_seen_at"].isoformat()
    out["cover"] = media_url(d["cover_sha"])
    if len(row) > len(_NAMES) and row[len(_NAMES)] is not None:
        out["score"] = round(float(row[len(_NAMES)]), 4)
    return out
