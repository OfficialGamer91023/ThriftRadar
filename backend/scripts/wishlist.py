"""Manage wishlists from the terminal until the web app exists. Spec: DESIGN.md §4.8 `wishlist.py`.

Run from backend/:
  uv run python -m scripts.wishlist add "white sneakers size 42 under 5000" [--size 42] [--max-price 5000] [--brand Nike]
  uv run python -m scripts.wishlist add --image path/to/photo.jpg [--size 42]
  uv run python -m scripts.wishlist list
  uv run python -m scripts.wishlist matches ID
  uv run python -m scripts.wishlist remove ID
  uv run python -m scripts.wishlist test-notify        # one test macOS notification
Prints listing fields only (brand, model, size, price), never seller data.
"""

import argparse
import sys
from pathlib import Path

import psycopg
from pgvector.psycopg import register_vector

from app.matching import create_wishlist, wishlist_filters
from app.notify import get_notifier
from app.settings import Settings

OWNER = "local"


def _line(r) -> str:
    brand = " ".join(x for x in (r["brand"], r["model"]) if x) or "?"
    size = r["size_label"] or "size unknown"
    if r["size_label"] and (r["sources"] or {}).get("size") == "vlm":
        size += " (AI-read)"
    price = f"Rs {r['price_amount']:,}" if r["price_amount"] is not None else "price unknown"
    return f"  #{r['id']:<5} {brand:34.34} {size:20} {price:14} score {r['score']:.3f}"


def cmd_add(args, settings: Settings) -> int:
    if not args.text and not args.image:
        print("give a text or --image", file=sys.stderr)
        return 2
    from app.pipeline.models import ModelRegistry

    models = ModelRegistry(settings.models_dir, demo=False)
    if args.image:
        models.load()
        from app.api.search import embed_photo

        emb, jpeg, sha = embed_photo(models, Path(args.image).read_bytes())
        from app.media_store import LocalDirStore

        LocalDirStore(settings.media_dir).put(sha, jpeg)
        min_score = args.min_score if args.min_score is not None else settings.match_min_image
    else:
        from app.pipeline.embed import Embedder

        emb = Embedder(Path(settings.models_dir) / "siglip").embed_text([args.text])[0]
        sha = None
        min_score = args.min_score if args.min_score is not None else settings.match_min_text
    filters = wishlist_filters(args.text, size=args.size, max_price=args.max_price, brand=args.brand)
    with psycopg.connect(settings.database_url) as conn:
        register_vector(conn)
        w = create_wishlist(conn, owner=OWNER, embedding=emb, min_score=min_score, text=args.text,
                            ref_image_sha=sha, filters=filters)
        conn.commit()
    size = filters["size_eu_min"]
    print(f"wishlist #{w.id}: brand={filters['brand'] or 'any'} size={f'EU {size}' if size else 'any'} "
          f"max_price={filters['max_price'] or 'any'} min_score={min_score}")
    print(f"{w.matched} existing listings already match (no notifications for those; "
          f"`matches {w.id}` lists them). New posts that match will notify you.")
    return 0


def cmd_list(args, settings: Settings) -> int:
    with psycopg.connect(settings.database_url) as conn:
        rows = conn.execute(
            """SELECT w.id, coalesce(w.query_text, '(photo)'), w.brand, w.size_eu_min, w.max_price, w.active,
                      (SELECT count(*) FROM matches m WHERE m.wishlist_id = w.id)
               FROM wishlists w WHERE w.owner = %s ORDER BY w.id""", (OWNER,)).fetchall()
    for wid, text, brand, size, price, active, n in rows:
        print(f"#{wid:<4} {text[:40]:40} brand={brand or 'any'} size={size or 'any'} max={price or 'any'} "
              f"{'' if active else '(inactive) '}matches={n}")
    if not rows:
        print("no wishlists")
    return 0


def cmd_matches(args, settings: Settings) -> int:
    with psycopg.connect(settings.database_url) as conn:
        rows = conn.execute(
            """SELECT l.id, l.brand, l.model, l.size_label, l.price_amount, l.attr_sources, m.score
               FROM matches m JOIN listings l ON l.id = m.listing_id
               WHERE m.wishlist_id = %s ORDER BY m.score DESC LIMIT %s""", (args.id, args.limit)).fetchall()
    keys = ("id", "brand", "model", "size_label", "price_amount", "sources", "score")
    for r in rows:
        print(_line(dict(zip(keys, r))))
    if not rows:
        print("no matches")
    return 0


def cmd_remove(args, settings: Settings) -> int:
    with psycopg.connect(settings.database_url) as conn:
        n = conn.execute("DELETE FROM wishlists WHERE id = %s AND owner = %s", (args.id, OWNER)).rowcount
        conn.commit()
    print("removed" if n else "not found")
    return 0 if n else 1


def cmd_test_notify(args, settings: Settings) -> int:
    notifier = get_notifier(settings)
    ok = notifier.send("ThriftRadar", "Test notification: wishlist matches will look like this.")
    print(f"sent with {type(notifier).__name__}: {'ok' if ok else 'failed'}")
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m scripts.wishlist")
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("add")
    a.add_argument("text", nargs="?")
    a.add_argument("--image")
    a.add_argument("--size", type=float)
    a.add_argument("--max-price", type=int)
    a.add_argument("--brand")
    a.add_argument("--min-score", type=float)
    sub.add_parser("list")
    m = sub.add_parser("matches")
    m.add_argument("id", type=int)
    m.add_argument("--limit", type=int, default=20)
    r = sub.add_parser("remove")
    r.add_argument("id", type=int)
    sub.add_parser("test-notify")
    args = ap.parse_args(argv)
    settings = Settings()
    if settings.demo_mode:
        print("refusing: local mode only", file=sys.stderr)
        return 2
    return {"add": cmd_add, "list": cmd_list, "matches": cmd_matches, "remove": cmd_remove,
            "test-notify": cmd_test_notify}[args.cmd](args, settings)


if __name__ == "__main__":
    sys.exit(main())
