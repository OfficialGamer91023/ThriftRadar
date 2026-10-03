"""Put posts back in the queue. Spec: DESIGN.md §4.8 `requeue.py`. Dry-run unless --yes; refuses in demo mode.

Run from backend/:
  uv run python -m scripts.requeue --failed [--source S] [--limit N] [--yes]
  uv run python -m scripts.requeue --local --source chat_export [--yes]   # redo the local stage after recalibrating
  uv run python -m scripts.requeue --vlm --source chat_export [--limit N] [--yes]   # send finished posts to the VLM
"""

import argparse
import sys

import psycopg

from app.pipeline.decide import SegFlags, needs_vlm
from app.settings import Settings

# Typical call (DESIGN §3.1): ~2.2k input and ~250 output tokens, +10% for retries and repairs.
EST_IN_TOK, EST_OUT_TOK, EST_OVERHEAD = 2200, 250, 1.1
ATTRS = ("brand", "model", "colour", "condition", "size_label", "price_amount", "price_on_request")

LOCAL_STATES = ("done", "awaiting_vlm", "failed")


def requeue_failed(conn: psycopg.Connection, source: str | None, limit: int | None, apply: bool) -> int:
    ids = [r[0] for r in conn.execute(
        """SELECT id FROM posts WHERE status = 'failed' AND (%(src)s::text IS NULL OR source = %(src)s)
           ORDER BY id LIMIT %(lim)s""", {"src": source, "lim": limit}).fetchall()]
    if apply and ids:
        conn.execute("""UPDATE posts SET status = 'received', attempts = 0, last_error = NULL, next_attempt_at = now()
                        WHERE id = ANY(%s) AND status = 'failed'""", (ids,))
    return len(ids)


class Refused(Exception):
    pass


def requeue_local(conn: psycopg.Connection, source: str, apply: bool) -> dict[str, int]:
    busy = conn.execute("SELECT count(*) FROM posts WHERE source = %s AND status = 'processing'",
                        (source,)).fetchone()[0]
    if busy:
        raise Refused(f"{busy} {source} posts are being processed; stop the worker first")
    orphans = conn.execute(
        """SELECT count(*) FROM listing_sightings s
           JOIN listings l ON l.id = s.listing_id JOIN posts lp ON lp.id = l.post_id
           JOIN posts sp ON sp.id = s.post_id
           WHERE lp.source = %s AND lp.status = ANY(%s) AND sp.source <> %s""",
        (source, list(LOCAL_STATES), source)).fetchone()[0]
    if orphans:
        raise Refused(f"{orphans} sightings from other sources point at {source} listings; requeue those too")
    counts = dict(zip(("posts", "listings", "sightings"), conn.execute(
        """SELECT count(DISTINCT p.id), (SELECT count(*) FROM listings l JOIN posts q ON q.id = l.post_id
                                          WHERE q.source = %(s)s AND q.status = ANY(%(st)s)),
                  (SELECT count(*) FROM listing_sightings x JOIN posts q ON q.id = x.post_id
                    WHERE q.source = %(s)s AND q.status = ANY(%(st)s))
           FROM posts p WHERE p.source = %(s)s AND p.status = ANY(%(st)s)""",
        {"s": source, "st": list(LOCAL_STATES)}).fetchone()))
    if apply:
        with conn.transaction():
            conn.execute("CREATE TEMP TABLE rq ON COMMIT DROP AS SELECT id FROM posts "
                         "WHERE source = %s AND status = ANY(%s) FOR UPDATE", (source, list(LOCAL_STATES)))
            conn.execute("DELETE FROM listing_sightings WHERE post_id IN (SELECT id FROM rq)")
            conn.execute("DELETE FROM listings WHERE post_id IN (SELECT id FROM rq)")
            conn.execute("""UPDATE images SET segment_idx = NULL, detections = NULL, primary_box = NULL,
                                   embedding = NULL, ocr_text = NULL, ocr_ran = false
                            WHERE post_id IN (SELECT id FROM rq)""")
            conn.execute("""UPDATE posts SET status = 'received', stage = NULL, outcome = NULL, attempts = 0,
                                   last_error = NULL, next_attempt_at = now()
                            WHERE id IN (SELECT id FROM rq)""")
    return counts


def requeue_vlm(conn: psycopg.Connection, source: str, limit: int | None, provider: str, required: list[str],
                apply: bool) -> dict[str, int]:
    """done + vlm_policy='never' posts whose listings still miss fields -> awaiting_vlm with vlm_policy='auto'."""
    posts = [r[0] for r in conn.execute(
        """SELECT id FROM posts WHERE status = 'done' AND vlm_policy = 'never' AND source = %s
             AND EXISTS (SELECT 1 FROM listings l WHERE l.post_id = posts.id AND l.extraction = 'local')
           ORDER BY id""", (source,)).fetchall()]
    picked: dict[int, list[tuple[int, list[str]]]] = {}
    for pid in posts:
        if limit is not None and len(picked) >= limit:
            break
        wanted = []
        rows = conn.execute(
            f"""SELECT l.id, {", ".join("l." + a for a in ATTRS)},
                       EXISTS (SELECT 1 FROM images i WHERE i.post_id = l.post_id AND i.segment_idx = l.segment_idx
                                 AND jsonb_array_length(coalesce(i.detections, '[]')) > 2)
                FROM listings l WHERE l.post_id = %s AND l.extraction = 'local' AND l.item_idx = 0""",
            (pid,)).fetchall()
        for row in rows:
            attrs = dict(zip(ATTRS, row[1:-1]))
            d = needs_vlm(attrs, SegFlags(multi_item=row[-1]), "auto", provider, required)
            if d.needed:
                wanted.append((row[0], d.missing))
        if wanted:
            picked[pid] = wanted
    if apply and picked:
        with conn.transaction():
            for pid, wanted in picked.items():
                for lid, missing in wanted:
                    conn.execute("UPDATE listings SET vlm_missing = %s WHERE id = %s", (missing, lid))
                conn.execute("""UPDATE posts SET status = 'awaiting_vlm', vlm_policy = 'auto', vlm_attempts = 0,
                                       next_attempt_at = now(), last_error = NULL
                                WHERE id = %s AND status = 'done' AND vlm_policy = 'never'""", (pid,))
    return {"posts": len(picked), "calls": sum(len(w) for w in picked.values())}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m scripts.requeue")
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--failed", action="store_true")
    mode.add_argument("--local", action="store_true")
    mode.add_argument("--vlm", action="store_true")
    ap.add_argument("--source")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--yes", action="store_true", help="apply (default is a dry run)")
    args = ap.parse_args(argv)

    settings = Settings()
    if settings.demo_mode:
        print("refusing: requeue never runs in demo mode", file=sys.stderr)
        return 2
    with psycopg.connect(settings.database_url) as conn:
        role = conn.execute("SELECT value FROM db_meta WHERE key = 'role'").fetchone()[0]
        if role != "local":
            print(f"refusing: db_meta.role is {role!r}", file=sys.stderr)
            return 2
        verb = "requeued" if args.yes else "would requeue (dry run; add --yes)"
        if args.vlm:
            if not args.source:
                ap.error("--vlm needs --source")
            if settings.vlm_provider == "off":
                print("refusing: VLM_PROVIDER is off", file=sys.stderr)
                return 2
            c = requeue_vlm(conn, args.source, args.limit, settings.vlm_provider, settings.required_fields, args.yes)
            per_call = (EST_IN_TOK * settings.vlm_price_in_per_m + EST_OUT_TOK * settings.vlm_price_out_per_m) / 1e6
            print(f"{verb}: {c['posts']} posts, about {c['calls']} VLM calls; estimated "
                  f"${c['calls'] * per_call * EST_OVERHEAD:.2f} on {settings.vlm_provider} {settings.vlm_model} "
                  f"(daily cap {settings.vlm_daily_cap} calls)")
        elif args.failed:
            n = requeue_failed(conn, args.source, args.limit, args.yes)
            print(f"{verb}: {n} failed posts")
        else:
            if not args.source:
                ap.error("--local needs --source")
            try:
                counts = requeue_local(conn, args.source, args.yes)
            except Refused as e:
                print(f"refusing: {e}", file=sys.stderr)
                return 2
            print(f"{verb}: {counts['posts']} posts; delete {counts['listings']} listings, "
                  f"{counts['sightings']} sightings")
        conn.commit()
    return 0


if __name__ == "__main__":
    sys.exit(main())
