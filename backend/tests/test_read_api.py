"""Search, listings, media, wishlists and status APIs (local mode, fake models). Spec: DESIGN.md §4.7."""

import psycopg
import pytest
from fastapi.testclient import TestClient

from app import db
from app.main import create_app
from app.media_store import LocalDirStore
from app.settings import Settings
from app.worker import LocalCtx, claim_local, process_local
from tests.fakes import FakeModels, make_post
from tests.imgutil import encode, pattern

BASE = "http://127.0.0.1:8000"
H = {"X-ThriftRadar": "1"}


@pytest.fixture
def env(db_url, tmp_path):
    with psycopg.connect(db_url) as c:
        c.execute("TRUNCATE posts, wishlists RESTART IDENTITY CASCADE")
        c.commit()
    s = Settings(_env_file=None, database_url=db_url, ingest_token="t", sender_hmac_key="k",
                 media_dir=str(tmp_path / "media"), vlm_provider="off", load_models=False, match_min_text=-1)
    app = create_app(s)
    models = FakeModels()
    app.state.models = models
    store = LocalDirStore(s.media_dir)
    with psycopg.connect(db_url) as c:
        for i, cap in enumerate(["Nike AF1 size 42 Rs 4000", "Adidas samba size 43 Rs 6000", "Vans size 40"]):
            make_post(c, store, f"p{i}", [30 + i, 40 + i], caption=cap, vlm_policy="never")
    ctx = LocalCtx(app.state.pool, s, models, store)
    for _ in range(3):
        with db.tx(app.state.pool) as c:
            post = claim_local(c, 600, 3)
        process_local(ctx, post)
    with TestClient(app, base_url=BASE) as client:
        yield client, models


def test_search_text_with_filters_from_the_query(env):
    client, _ = env
    r = client.get("/api/search", params={"q": "nike size 42 under 5000"})
    assert r.status_code == 200
    body = r.json()
    assert body["filters"] == {"brand": "Nike", "max_price": 5000, "size_eu": 42.0}
    assert [x["brand"] for x in body["results"]] == ["Nike"]
    hit = body["results"][0]
    assert hit["cover"].startswith("/media/") and hit["size_label"] == "EU 42"
    assert body["order"] == "newest" and "score" not in hit  # only filters: no similarity to rank by
    assert len(client.get("/api/search", params={"q": "shoes"}).json()["results"]) == 3


def test_search_with_descriptive_words_ranks_by_similarity(env):
    client, _ = env
    body = client.get("/api/search", params={"q": "white sneakers"}).json()
    assert body["order"] == "similar" and all("score" in r for r in body["results"])


def test_size_only_search_and_exact_sizes_first(env, db_url):
    client, _ = env
    with psycopg.connect(db_url) as c:  # one AI-read size within the slack, one unknown size
        c.execute("""UPDATE listings SET size_eu = 41, size_label = 'EU 41', attr_sources = attr_sources || '{"size":"vlm"}'
                     WHERE brand = 'Nike'""")
        c.execute("UPDATE listings SET size_eu = NULL, size_label = NULL WHERE brand = 'Adidas'")
        c.commit()
    body = client.get("/api/search", params={"size": 40}).json()
    assert body["filters"]["size_eu"] == 40.0 and body["order"] == "newest"
    assert [r["brand"] for r in body["results"]] == ["Vans", "Nike", "Adidas"]  # exact, AI-near, unknown
    assert client.get("/api/search").status_code == 400  # nothing to search for


def test_search_needs_models(env):
    client, models = env
    models.ready.clear()
    assert client.get("/api/search", params={"q": "x"}).status_code == 503


def test_image_search_stores_nothing(env, db_url):
    client, _ = env
    r = client.post("/api/search/image", files={"file": ("a.jpg", encode(pattern(30)), "image/jpeg")}, headers=H)
    assert r.status_code == 200 and len(r.json()["results"]) == 3  # ranking quality is the real model's job
    assert client.post("/api/search/image", files={"file": ("a.jpg", b"not an image", "image/jpeg")},
                       headers=H).status_code == 422
    with psycopg.connect(db_url) as c:
        assert c.execute("SELECT count(*) FROM images").fetchone()[0] == 6


def test_listings_feed_detail_and_media(env):
    client, _ = env
    page = client.get("/api/listings", params={"limit": 2}).json()
    assert len(page["results"]) == 2 and page["next"]
    rest = client.get("/api/listings", params={"limit": 2, "cursor": page["next"]}).json()
    assert len(rest["results"]) == 1 and rest["next"] is None
    assert client.get("/api/listings", params={"cursor": "junk"}).status_code == 400
    lid = page["results"][0]["id"]
    d = client.get(f"/api/listings/{lid}").json()
    assert len(d["images"]) == 2 and d["sightings"][0]["kind"] == "origin"
    img = client.get(d["images"][0])
    assert img.status_code == 200 and img.headers["content-type"] == "image/jpeg"
    assert "immutable" in img.headers["cache-control"]
    assert client.get("/media/" + "0" * 64).status_code == 404
    assert client.get("/media/../etc/passwd").status_code == 404
    assert client.get("/api/listings/999999").status_code == 404


def test_wishlists_crud(env):
    client, _ = env
    assert client.post("/api/wishlists", json={"text": "nike 42"}).status_code == 403  # no X-ThriftRadar
    r = client.post("/api/wishlists", json={"text": "nike size 42 under 5000"}, headers=H)
    assert r.status_code == 201
    w = r.json()
    assert (w["brand"], w["size_eu"], w["max_price"], w["matches"]) == ("Nike", 42.0, 5000, 1)
    assert client.get("/api/wishlists").json()["results"][0]["id"] == w["id"]
    detail = client.get(f"/api/wishlists/{w['id']}").json()
    assert [x["brand"] for x in detail["results"]] == ["Nike"]
    assert client.delete(f"/api/wishlists/{w['id']}", headers=H).status_code == 204
    assert client.get(f"/api/wishlists/{w['id']}").status_code == 404


def test_image_wishlist(env):
    client, _ = env
    r = client.post("/api/wishlists/image", files={"file": ("a.jpg", encode(pattern(31)), "image/jpeg")},
                    data={"size": "43"}, headers=H)
    assert r.status_code == 201 and r.json()["image"] and r.json()["size_eu"] == 43.0


def test_status(env):
    client, _ = env
    s = client.get("/api/status").json()
    assert s["queue"] == {"done": 3} and s["vlm"]["provider"] == "off" and s["vlm"]["paused"] is False
