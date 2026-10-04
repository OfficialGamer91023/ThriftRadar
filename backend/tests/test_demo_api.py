"""Demo mode: login, sessions, visibility, rate limits, uploads, purge. Spec: DESIGN.md §4.7, §6.3 "Demo API"."""

import uuid

import psycopg
import pytest
from fastapi.testclient import TestClient

from app import db
from app.main import create_app
from app.media_store import LocalDirStore
from app.ratelimit import SlidingWindowLimiter
from app.sessions import sign, unsign
from app.settings import Settings
from app.worker import LocalCtx, claim_local, process_local, purge_demo_uploads
from tests.fakes import FakeModels, make_post
from tests.imgutil import encode, pattern

HTTPS = "https://demo.test"  # the session cookie is Secure
CREDS = {"email": "demo@thriftradar.app", "password": "demo1234"}
H = {"X-ThriftRadar": "1"}


def demo_settings(url, tmp_path, **kw) -> Settings:
    base = dict(DEMO_MODE="1", database_url=url, session_secret="sess", sender_hmac_key="hk",
                vlm_provider="off", load_models=False, seed_dir=str(tmp_path / "no-seed"), match_min_text=-1)
    return Settings(_env_file=None, **(base | kw))


@pytest.fixture
def demo(demo_db_url, tmp_path):
    with psycopg.connect(demo_db_url) as c:
        c.execute("TRUNCATE posts, wishlists, media_blobs, vlm_calls RESTART IDENTITY CASCADE")
        c.commit()
    app = create_app(demo_settings(demo_db_url, tmp_path))
    app.state.models = FakeModels()
    with TestClient(app, base_url=HTTPS) as client:
        yield app, client


def login(app) -> TestClient:
    c = TestClient(app, base_url=HTTPS)
    assert c.post("/api/login", json=CREDS, headers=H).status_code == 200
    return c


def upload(client, seeds=(1,), caption="Nike AF1 size 42 Rs 4000", upload_id=None, ip=None):
    files = [("files", (f"{s}.jpg", encode(pattern(s)), "image/jpeg")) for s in seeds]
    headers = H | {"X-Upload-Id": upload_id or str(uuid.uuid4())}
    if ip:
        headers["X-Forwarded-For"] = ip
    return client.post("/api/demo/posts", files=files, data={"caption": caption}, headers=headers)


def process_all(app):
    ctx = LocalCtx(app.state.pool, app.state.settings, app.state.models, app.state.media_store)
    while True:
        with db.tx(app.state.pool) as c:
            post = claim_local(c, 600, 3)
        if post is None:
            return
        process_local(ctx, post)


def seed_listing(app, demo_db_url):
    with psycopg.connect(demo_db_url) as c:
        make_post(c, app.state.media_store, "seed:vans", [70], caption="Vans old skool size 41 Rs 3000",
                  source="demo_seed", vlm_policy="never",
                  seed_attrs={"brand": "Vans", "model": "Old Skool", "size_label": "EU 41", "size_eu": 41,
                              "price": 3000, "currency": "PKR"})
    process_all(app)


# ---------- sessions ----------

def test_signed_session_round_trip_and_tamper():
    sid = str(uuid.uuid4())
    tok = sign("k", sid, now=1000)
    assert unsign("k", tok, now=1000 + 60) == sid
    assert unsign("k", tok, now=1000 + 86401) is None  # older than 24 h
    assert unsign("other", tok, now=1000) is None
    assert unsign("k", tok.replace(sid, str(uuid.uuid4())), now=1000) is None
    assert unsign("k", "garbage", now=1000) is None


@pytest.mark.parametrize("raw", [None, "0", "true"])
def test_login_404_unless_demo_mode_is_exactly_1(db_url, tmp_path, raw):
    kw = dict(database_url=db_url, ingest_token="t", sender_hmac_key="k", media_dir=str(tmp_path),
              vlm_provider="off", load_models=False, session_secret="s")
    if raw is not None:
        kw["DEMO_MODE"] = raw
    app = create_app(Settings(_env_file=None, **kw))
    with TestClient(app, base_url="http://127.0.0.1:8000") as c:
        assert c.post("/api/login", json=CREDS, headers=H).status_code == 404
        assert c.post("/api/demo/posts", headers=H).status_code == 404


def test_login_sets_cookie_and_wrong_password_is_401(demo):
    app, client = demo
    r = client.post("/api/login", json=CREDS | {"email": " Demo@ThriftRadar.app "}, headers=H)
    assert r.status_code == 200
    cookie = r.headers["set-cookie"]
    assert "tr_session=" in cookie and "HttpOnly" in cookie and "Secure" in cookie and "samesite=lax" in cookie.lower()
    assert client.get("/api/session").json() == {"logged_in": True}
    assert client.post("/api/login", json=CREDS | {"password": "nope"}, headers=H).status_code == 401
    client.post("/api/logout", headers=H)
    assert client.get("/api/session").json() == {"logged_in": False}


def test_eleventh_login_in_a_minute_is_429(demo):
    _, client = demo
    codes = [client.post("/api/login", json=CREDS, headers=H).status_code for _ in range(11)]
    assert codes[:10] == [200] * 10 and codes[10] == 429


@pytest.mark.parametrize("method,path", [
    ("GET", "/api/search?q=nike"), ("GET", "/api/listings"), ("GET", "/api/listings/1"), ("GET", "/api/posts/1"),
    ("GET", "/api/wishlists"), ("POST", "/api/wishlists"), ("GET", "/api/status"), ("POST", "/api/demo/posts"),
    ("GET", "/media/" + "0" * 64)])
def test_api_needs_a_session(demo, method, path):
    _, client = demo
    r = client.request(method, path, headers=H, json={"text": "x"} if path == "/api/wishlists" else None)
    assert r.status_code == 401
    assert client.get("/api/config").json() == {"demo": True, "features": {"whatsapp": False}}
    assert client.get("/healthz").status_code == 200


@pytest.mark.parametrize("method", ["GET", "POST", "PUT", "PATCH", "DELETE"])
@pytest.mark.parametrize("path", ["/ingest", "/admin/queue", "/listener/heartbeat", "/api/listener/status",
                                  "/api/listener/anything"])
def test_local_only_paths_404_in_demo_even_logged_in(demo, method, path):
    app, _ = demo
    c = login(app)
    assert c.request(method, path, headers=H | {"X-Ingest-Token": "anything"}).status_code == 404


# ---------- uploads ----------

def test_upload_is_processed_and_visible_only_to_its_session(demo, demo_db_url):
    app, _ = demo
    seed_listing(app, demo_db_url)
    a, b = login(app), login(app)
    r = upload(a)
    assert r.status_code == 202
    post_id = r.json()["post_id"]
    process_all(app)

    mine = a.get(f"/api/posts/{post_id}").json()
    assert mine["status"] == "done" and mine["listings"][0]["brand"] == "Nike"
    lid, cover = mine["listings"][0]["id"], mine["listings"][0]["cover"]
    assert a.get(cover).status_code == 200  # the photo comes from media_blobs

    assert b.get(f"/api/posts/{post_id}").status_code == 404
    assert b.get(f"/api/listings/{lid}").status_code == 404
    assert b.get(cover).status_code == 404
    assert [x["brand"] for x in b.get("/api/listings").json()["results"]] == ["Vans"]
    assert [x["brand"] for x in b.get("/api/search", params={"q": "shoes"}).json()["results"]] == ["Vans"]
    assert {x["brand"] for x in a.get("/api/search", params={"q": "shoes"}).json()["results"]} == {"Vans", "Nike"}
    with psycopg.connect(demo_db_url) as c:
        seed_post = c.execute("SELECT id FROM posts WHERE source = 'demo_seed'").fetchone()[0]
        assert c.execute("SELECT sender_jid, owner_session IS NOT NULL FROM posts WHERE id = %s",
                         (post_id,)).fetchone() == (None, True)
    assert a.get(f"/api/posts/{seed_post}").status_code == 404  # post status is owner-only


def test_same_upload_id_twice_gives_the_same_post(demo):
    app, _ = demo
    a = login(app)
    uid = str(uuid.uuid4())
    first = upload(a, upload_id=uid)
    again = upload(a, upload_id=uid)
    assert first.status_code == 202 and again.status_code == 200
    assert again.json() == {"post_id": first.json()["post_id"], "duplicate": True}
    # another session reusing the id gets its own post (the key includes the session)
    other = upload(login(app), upload_id=uid)
    assert other.status_code == 202 and other.json()["post_id"] != first.json()["post_id"]


def test_upload_validation(demo):
    app, _ = demo
    a = login(app)
    r = a.post("/api/demo/posts", files=[("files", ("a.jpg", encode(pattern(1)), "image/jpeg"))],
               headers=H | {"X-Upload-Id": "not-a-uuid"})
    assert r.status_code == 400
    assert upload(a, seeds=()).status_code == 422
    assert upload(a, caption="x" * 501).status_code == 422
    bad = a.post("/api/demo/posts", files=[("files", ("a.jpg", b"not an image", "image/jpeg"))],
                 headers=H | {"X-Upload-Id": str(uuid.uuid4())})
    assert bad.status_code == 422


def test_six_mb_file_is_413(demo):
    app, _ = demo
    a = login(app)
    big = b"\xff\xd8\xff" + b"0" * (6 * 1024 * 1024)
    r = a.post("/api/demo/posts", files=[("files", ("big.jpg", big, "image/jpeg"))],
               headers=H | {"X-Upload-Id": str(uuid.uuid4())})
    assert r.status_code == 413


def test_sixth_upload_in_an_hour_from_one_ip_is_429(demo):
    app, _ = demo
    codes = [upload(login(app), seeds=(i,), ip="9.9.9.9").status_code for i in range(6)]
    assert codes == [202] * 5 + [429]
    assert upload(login(app), seeds=(7,), ip="8.8.8.8").status_code == 202  # another IP is fine


def test_eleventh_upload_in_a_day_from_one_session_is_429(demo):
    app, _ = demo
    a = login(app)
    codes = [upload(a, seeds=(i,), ip=f"10.0.0.{i}").status_code for i in range(11)]
    assert codes == [202] * 10 + [429]


def test_global_upload_cap_counts_uploads_already_in_the_db(demo_db_url, tmp_path):
    with psycopg.connect(demo_db_url) as c:
        c.execute("TRUNCATE posts, wishlists, media_blobs RESTART IDENTITY CASCADE")
        for i in range(3):
            make_post(c, None, f"up{i}", [i], source="demo_upload")
    app = create_app(demo_settings(demo_db_url, tmp_path))
    lim: SlidingWindowLimiter = app.state.limiter
    assert lim.check("upload_global", "all", 3, 86400)[0] is False
    assert lim.check("upload_global", "all", 4, 86400) == (True, 0)
    app.state.pool.close()


# ---------- wishlists, search limits ----------

def test_wishlists_belong_to_the_session(demo, demo_db_url):
    app, _ = demo
    seed_listing(app, demo_db_url)
    a, b = login(app), login(app)
    r = a.post("/api/wishlists", json={"text": "vans size 41"}, headers=H)
    assert r.status_code == 201 and r.json()["matches"] == 1
    wid = r.json()["id"]
    assert [w["id"] for w in a.get("/api/wishlists").json()["results"]] == [wid]
    assert b.get("/api/wishlists").json()["results"] == []
    assert b.get(f"/api/wishlists/{wid}").status_code == 404
    assert b.delete(f"/api/wishlists/{wid}", headers=H).status_code == 404


def test_a_wishlist_never_matches_another_sessions_upload(demo):
    app, _ = demo
    a, b = login(app), login(app)
    assert b.post("/api/wishlists", json={"text": "nike size 42"}, headers=H).json()["matches"] == 0
    upload(a)
    process_all(app)
    wid = b.get("/api/wishlists").json()["results"][0]["id"]
    assert b.get(f"/api/wishlists/{wid}").json()["results"] == []
    own = a.post("/api/wishlists", json={"text": "nike size 42"}, headers=H).json()
    assert own["matches"] == 1


def test_search_rate_limit_is_per_ip(demo):
    app, _ = demo
    a = login(app)
    codes = [a.get("/api/search", params={"q": "nike"}, headers={"X-Forwarded-For": "5.5.5.5"}).status_code
             for _ in range(61)]
    assert codes[:60] == [200] * 60 and codes[60] == 429
    r = a.get("/api/search", params={"q": "nike"}, headers={"X-Forwarded-For": "5.5.5.5"})
    assert int(r.headers["Retry-After"]) >= 1


def test_spoofed_left_xff_entries_do_not_dodge_the_limit(demo):
    app, _ = demo
    a = login(app)
    codes = [a.get("/api/search", params={"q": "x"},
                   headers={"X-Forwarded-For": f"1.1.1.{i}, 5.5.5.6"}).status_code for i in range(61)]
    assert codes[60] == 429


# ---------- purge ----------

def test_purge_removes_day_old_uploads_and_blobs_and_keeps_seed(demo, demo_db_url):
    app, _ = demo
    seed_listing(app, demo_db_url)
    a = login(app)
    old, new = upload(a, seeds=(11,)).json()["post_id"], upload(a, seeds=(12,)).json()["post_id"]
    a.post("/api/wishlists", json={"text": "nike"}, headers=H)
    process_all(app)
    with psycopg.connect(demo_db_url) as c:
        c.execute("UPDATE posts SET created_at = now() - interval '25 hours' WHERE id = %s", (old,))
        c.execute("UPDATE posts SET created_at = now() - interval '23 hours' WHERE id = %s", (new,))
        c.execute("UPDATE media_blobs SET created_at = now() - interval '25 hours'")
        c.execute("UPDATE wishlists SET created_at = now() - interval '25 hours'")
        counts = purge_demo_uploads(c)
        c.commit()
        assert counts == {"posts": 1, "wishlists": 1, "blobs": 1}
        left = {r[0] for r in c.execute("SELECT source FROM posts").fetchall()}
        assert left == {"demo_seed", "demo_upload"}
        assert c.execute("SELECT count(*) FROM posts WHERE id = %s", (old,)).fetchone()[0] == 0
        assert c.execute("SELECT count(*) FROM media_blobs").fetchone()[0] == 2  # seed photo + the 23 h upload


# ---------- limiter ----------

def test_sliding_window_limiter():
    t = [0.0]
    lim = SlidingWindowLimiter(max_keys=2, clock=lambda: t[0])
    assert [lim.hit("b", "k", 2, 10)[0] for _ in range(3)] == [True, True, False]
    assert lim.hit("b", "k", 2, 10) == (False, 10)
    t[0] = 10.5  # both hits slid out of the window
    assert lim.hit("b", "k", 2, 10) == (True, 0)
    lim.hit("b", "k2", 2, 10)
    lim.hit("b", "k3", 2, 10)  # evicts the least recently used key ("k")
    assert lim.check("b", "k", 1, 10) == (True, 0)
    lim.seed("g", "all", [100.0, 5.0])
    assert lim.check("g", "all", 2, 200)[0] is False
    assert lim.check("g", "all", 2, 50)[0] is True  # the 100 s old hit is outside a 50 s window


def test_an_upload_never_rewrites_a_seed_or_another_sessions_listing(demo, demo_db_url):
    """Same photo as the seed (pHash distance 0) and as A's upload: B's post must become its own listing."""
    app, _ = demo
    seed_listing(app, demo_db_url)  # pattern(70)
    a, b = login(app), login(app)
    upload(a, seeds=(70,), caption="Rs 1")
    process_all(app)
    upload(b, seeds=(70,), caption="Rs 2")
    process_all(app)
    with psycopg.connect(demo_db_url) as c:
        rows = c.execute("""SELECT source, price_amount, repost_count FROM listings ORDER BY id""").fetchall()
    assert rows == [("demo_seed", 3000, 0), ("demo_upload", None, 0), ("demo_upload", None, 0)]
    # a second upload of the same photo by A is A's own repost
    upload(a, seeds=(70,), caption="Rs 5000")
    process_all(app)
    with psycopg.connect(demo_db_url) as c:
        assert c.execute("SELECT count(*), max(repost_count) FROM listings WHERE source = 'demo_upload'"
                         ).fetchone() == (2, 1)


def test_credits_are_public(demo):
    _, client = demo  # no login
    body = client.get("/api/credits").json()
    assert body["results"] == []  # the fixture points seed_dir at an empty dir


def test_credits_list_the_real_seed(demo_db_url, tmp_path):
    from scripts.seed_demo import SEED_DIR

    app = create_app(demo_settings(demo_db_url, tmp_path, seed_dir=str(SEED_DIR)))
    with TestClient(app, base_url=HTTPS) as c:
        rows = c.get("/api/credits").json()["results"]
    assert len(rows) >= 40 and all(r["author"] and r["license"].startswith("CC") for r in rows)
