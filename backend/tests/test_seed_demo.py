"""seed_demo: license and PII gates, idempotency, role check. Spec: DESIGN.md §4.8, §6.4."""

import csv
import hashlib
import json

import psycopg
import pytest

from scripts import seed_demo
from tests.imgutil import encode, pattern

FIELDS = seed_demo.CSV_FIELDS


def make_seed(root, posts=2, caption="Nike AF1 size 42 Rs 4500", seller="Demo Seller 1"):
    images = root / "images"
    images.mkdir(parents=True)
    rows, mposts = [], []
    for i in range(posts):
        name = f"p{i}.jpg"
        data = encode(pattern(200 + i))
        (images / name).write_bytes(data)
        rows.append({"file": name, "sha256": hashlib.sha256(data).hexdigest(), "source_url": "https://example.org/x",
                     "author": "Someone", "license": "CC-BY-2.0", "license_url": "https://creativecommons.org/",
                     "notes": ""})
        mposts.append({"slug": f"post-{i}", "seller": seller, "caption": caption, "days_ago": i, "images": [name],
                       "attrs": {"brand": "Nike", "size_label": "EU 42", "size_eu": 42, "price": 4500,
                                 "currency": "PKR"}})
    (root / "manifest.json").write_text(json.dumps({"version": 1, "posts": mposts}))
    with open(root / "ATTRIBUTION.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows)
    return root


@pytest.fixture
def demo_db(demo_db_url, monkeypatch):
    with psycopg.connect(demo_db_url) as c:
        c.execute("TRUNCATE posts, wishlists, media_blobs RESTART IDENTITY CASCADE")
        c.commit()
    monkeypatch.setenv("SENDER_HMAC_KEY", "k")
    return demo_db_url


def run(url, root, *extra):
    return seed_demo.main(["--database-url", url, "--seed-dir", str(root), *extra])


def count(url):
    with psycopg.connect(url) as c:
        return c.execute("SELECT count(*), array_agg(id ORDER BY id) FROM posts WHERE source = 'demo_seed'").fetchone()


def test_run_twice_is_idempotent_and_refresh_replaces(demo_db, tmp_path):
    root = make_seed(tmp_path / "seed")
    assert run(demo_db, root) == 0
    n, ids = count(demo_db)
    assert n == 2
    assert run(demo_db, root) == 0 and count(demo_db) == (2, ids)
    assert run(demo_db, root, "--refresh") == 0
    n2, ids2 = count(demo_db)
    assert n2 == 2 and set(ids2).isdisjoint(ids)
    with psycopg.connect(demo_db) as c:
        assert c.execute("SELECT vlm_policy, seed_attrs->>'brand', sender_jid FROM posts LIMIT 1").fetchone() == (
            "never", "Nike", None)


def test_missing_license_row_refuses(demo_db, tmp_path):
    root = make_seed(tmp_path / "seed")
    lines = (root / "ATTRIBUTION.csv").read_text().splitlines()
    (root / "ATTRIBUTION.csv").write_text("\n".join(lines[:-1]) + "\n")
    assert run(demo_db, root) == 2 and count(demo_db)[0] == 0


def test_sha_mismatch_refuses(demo_db, tmp_path):
    root = make_seed(tmp_path / "seed")
    (root / "images" / "p0.jpg").write_bytes(encode(pattern(999)))
    assert run(demo_db, root) == 2


def test_disallowed_license_refuses(demo_db, tmp_path):
    root = make_seed(tmp_path / "seed")
    text = (root / "ATTRIBUTION.csv").read_text().replace("CC-BY-2.0", "CC-BY-NC-2.0", 1)
    (root / "ATTRIBUTION.csv").write_text(text)
    assert run(demo_db, root) == 2


@pytest.mark.parametrize("caption", ["call 0300 1234567", "msg +92 300-123-4567", "wa.me/923001234567",
                                     "see https://shop.example"])
def test_contact_details_in_a_caption_refuse(demo_db, tmp_path, caption):
    assert run(demo_db, make_seed(tmp_path / "seed", caption=caption)) == 2


def test_real_looking_seller_name_refuses(demo_db, tmp_path):
    assert run(demo_db, make_seed(tmp_path / "seed", seller="Ali Shoes")) == 2


def test_local_db_refused_without_flag(db_url, tmp_path, monkeypatch):
    monkeypatch.setenv("SENDER_HMAC_KEY", "k")
    assert run(db_url, make_seed(tmp_path / "seed")) == 2


def test_real_seed_passes_the_gates():
    """The committed seed: every photo licensed, sha256s match, no contact details."""
    m = seed_demo.load_manifest(seed_demo.SEED_DIR / "manifest.json")
    seed_demo.validate_licenses(m, seed_demo.SEED_DIR / "ATTRIBUTION.csv", seed_demo.SEED_DIR / "images")
    seed_demo.scan_pii(m)
    assert 40 <= len(m.posts) <= 60
