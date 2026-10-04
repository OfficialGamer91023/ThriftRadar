"""Load the licensed demo seed into the demo DB. Spec: DESIGN.md §4.8 `seed_demo.py`.

Run from backend/:  uv run python -m scripts.seed_demo [--database-url URL] [--refresh] [--allow-local-db]
Every image must have a license row (and a matching sha256) in seed/ATTRIBUTION.csv; seller names and captions are
scanned for anything that looks like real contact data. Any failure exits 2 before the DB is touched.
Seed posts carry ground-truth attributes and never use the VLM; the worker only detects and embeds them.
"""

import argparse
import csv
import hashlib
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psycopg
from pydantic import BaseModel, Field

from app.ids import chat_ref, sender_ref
from app.images import normalize_image
from app.ingest_service import ImageInput, MsgInput, PostInput, create_post
from app.media_store import LocalDirStore
from app.settings import Settings

SEED_DIR = Path(__file__).resolve().parents[1] / "seed"
LICENSES = {"CC0-1.0", "CC-BY-2.0", "CC-BY-4.0", "CC-BY-SA-2.0", "CC-BY-SA-4.0", "Unsplash", "Pexels", "Own"}
CSV_FIELDS = ["file", "sha256", "source_url", "author", "license", "license_url", "notes"]
SELLER = re.compile(r"^Demo Seller \d{1,2}$")
PII = [re.compile(r"\+?\d[\d\s\-()]{7,}\d"), re.compile(r"@s\.whatsapp\.net"), re.compile(r"wa\.me/"),
       re.compile(r"https?://|www\.", re.I)]
BUNDLED_MAX_SIDE = 2048  # must match BundledStore, so the normalized sha256s agree


class SeedError(Exception):
    pass


class Attrs(BaseModel):
    brand: str | None = None
    model: str | None = None
    colour: str | None = None
    size_label: str | None = None
    size_eu: float | None = None
    price: int | None = None
    currency: str | None = None
    condition: str | None = None


class SeedPost(BaseModel):
    slug: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{1,60}$")
    seller: str
    caption: str = Field(max_length=500)
    days_ago: float = Field(default=0, ge=0, le=60)
    images: list[str] = Field(min_length=1, max_length=4)
    attrs: Attrs


class Manifest(BaseModel):
    version: int
    posts: list[SeedPost]


def load_manifest(path: Path) -> Manifest:
    m = Manifest.model_validate_json(path.read_text())
    slugs = [p.slug for p in m.posts]
    if len(slugs) != len(set(slugs)):
        raise SeedError("duplicate slugs in manifest")
    return m


def validate_licenses(manifest: Manifest, csv_path: Path, images_dir: Path) -> dict[str, dict]:
    with open(csv_path, newline="") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames != CSV_FIELDS:
            raise SeedError(f"ATTRIBUTION.csv columns must be {CSV_FIELDS}")
        rows = {r["file"]: r for r in reader}
    on_disk = {p.name for p in images_dir.iterdir() if p.is_file() and not p.name.startswith(".")}
    used = {f for p in manifest.posts for f in p.images}
    problems = [f"{f}: no license row" for f in sorted(used - rows.keys())]
    problems += [f"{f}: on disk but not in ATTRIBUTION.csv" for f in sorted(on_disk - rows.keys())]
    problems += [f"{f}: in ATTRIBUTION.csv but not on disk" for f in sorted(rows.keys() - on_disk)]
    for name, r in rows.items():
        if r["license"] not in LICENSES:
            problems.append(f"{name}: license {r['license']!r} not allowed")
        if not r["author"].strip() or not r["source_url"].startswith("https://"):
            problems.append(f"{name}: author and an https source_url are required")
        if name in on_disk and hashlib.sha256((images_dir / name).read_bytes()).hexdigest() != r["sha256"]:
            problems.append(f"{name}: sha256 mismatch")
    if problems:
        raise SeedError("license check failed:\n  " + "\n  ".join(problems))
    return rows


def scan_pii(manifest: Manifest) -> None:
    problems = [f"{p.slug}: seller must look like 'Demo Seller N'" for p in manifest.posts if not SELLER.match(p.seller)]
    for p in manifest.posts:
        for rx in PII:
            if rx.search(p.caption):
                problems.append(f"{p.slug}: caption matches {rx.pattern!r}")
    if problems:
        raise SeedError("PII scan failed:\n  " + "\n  ".join(problems))


def seed(conn: psycopg.Connection, manifest: Manifest, images_dir: Path, key: str, refresh: bool = False,
         now: datetime | None = None, store=None) -> dict:
    """-> {"created": n, "duplicate": n}. `store` (tests, --allow-local-db) also receives the normalized photos."""
    now = now or datetime.now(timezone.utc)
    norm_cache: dict[str, object] = {}
    counts = {"created": 0, "duplicate": 0}
    with conn.transaction():
        if refresh:
            conn.execute("DELETE FROM posts WHERE source = 'demo_seed'")
        for p in manifest.posts:
            at = now - timedelta(days=p.days_ago)
            msgs = []
            for n, name in enumerate(p.images):
                if name not in norm_cache:
                    norm_cache[name] = normalize_image((images_dir / name).read_bytes(), max_side=BUNDLED_MAX_SIDE)
                norm = norm_cache[name]
                if store is not None:
                    store.put(norm.sha256, norm.jpeg)
                msgs.append(MsgInput(f"seed:{p.slug}:{n}", "image", at + timedelta(seconds=n),
                                     p.caption if n == 0 else None,
                                     ImageInput(norm.sha256, norm.phash, norm.width, norm.height, len(norm.jpeg))))
            r = create_post(conn, PostInput(
                source="demo_seed", idempotency_key=f"seed:{p.slug}", chat_ref=chat_ref("demo", key),
                sender_ref=sender_ref("demo:" + p.seller, key), messages=msgs, vlm_policy="never",
                seed_attrs=p.attrs.model_dump()))
            counts[r.status] += 1
    return counts


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m scripts.seed_demo")
    ap.add_argument("--database-url")
    ap.add_argument("--seed-dir", type=Path, default=SEED_DIR)
    ap.add_argument("--refresh", action="store_true")
    ap.add_argument("--allow-local-db", action="store_true")
    args = ap.parse_args(argv)
    settings = Settings()
    if not settings.sender_hmac_key:
        print("refusing: SENDER_HMAC_KEY is required", file=sys.stderr)
        return 2
    try:
        manifest = load_manifest(args.seed_dir / "manifest.json")
        validate_licenses(manifest, args.seed_dir / "ATTRIBUTION.csv", args.seed_dir / "images")
        scan_pii(manifest)
    except (SeedError, ValueError) as e:
        print(f"refusing: {e}", file=sys.stderr)
        return 2
    with psycopg.connect(args.database_url or settings.database_url) as conn:
        role = conn.execute("SELECT value FROM db_meta WHERE key = 'role'").fetchone()[0]
        if role != "demo" and not args.allow_local_db:
            print(f"refusing: db_meta.role is {role!r}; seed only the demo DB (or pass --allow-local-db)",
                  file=sys.stderr)
            return 2
        # demo: photos are served from the bundled seed files; a local test run copies them into data/media
        store = None if role == "demo" else LocalDirStore(settings.media_dir)
        counts = seed(conn, manifest, args.seed_dir / "images", settings.sender_hmac_key, args.refresh, store=store)
        conn.commit()
    print(f"seed: {counts['created']} created, {counts['duplicate']} already there "
          f"({len(manifest.posts)} posts in the manifest)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
