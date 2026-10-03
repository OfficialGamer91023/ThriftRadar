import json
from datetime import datetime, timedelta, timezone

import psycopg
import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request

from app.ids import idempotency_key
from app.main import StartupError, create_app
from app.settings import Settings
from tests.imgutil import encode, pattern

TOKEN = "test-ingest-token"
BASE = "http://127.0.0.1:8000"
T0 = datetime.now(timezone.utc) - timedelta(hours=1)


def _settings(db_url, tmp_path, **kw) -> Settings:
    base = dict(database_url=db_url, ingest_token=TOKEN, sender_hmac_key="hk", media_dir=str(tmp_path),
                vlm_provider="off", load_models=False)
    return Settings(_env_file=None, **(base | kw))


@pytest.fixture
def app(db_url, tmp_path):
    with psycopg.connect(db_url) as c:
        c.execute("TRUNCATE posts RESTART IDENTITY CASCADE")
        c.commit()
    application = create_app(_settings(db_url, tmp_path))
    woke = []
    application.state.wake = lambda: woke.append(1)
    application.state.woke = woke
    return application


@pytest.fixture
def client(app):
    with TestClient(app, base_url=BASE) as c:
        yield c


def album(keys, chat="123@g.us", source="whatsapp", captions=None, files=None, text_keys=()):
    """Build (headers, data, files) for /ingest. Every key is an image unless listed in text_keys."""
    messages, parts = [], []
    for i, k in enumerate(keys):
        if k in text_keys:
            messages.append({"key": k, "kind": "text", "sent_at": (T0 + timedelta(seconds=i)).isoformat(),
                             "caption": "dm me"})
            continue
        field = f"f{i}"
        messages.append({"key": k, "kind": "image", "sent_at": (T0 + timedelta(seconds=i)).isoformat(),
                         "caption": (captions or {}).get(k), "file_field": field})
        data = (files or {}).get(k, encode(pattern(hash(k) % 1000)))
        parts.append((field, (f"{field}.jpg", data, "image/jpeg")))
    meta = {"source": source, "chat_id": chat, "sender_id": "abc@lid",
            "sender_alt": "923001234567@s.whatsapp.net", "messages": messages}
    headers = {"X-Ingest-Token": TOKEN, "Idempotency-Key": idempotency_key(source, chat, list(keys))}
    return headers, {"meta": json.dumps(meta)}, parts


def post(client, keys, **kw):
    headers, data, files = album(keys, **kw)
    return client.post("/ingest", headers=headers, data=data, files=files)


def test_accepts_then_duplicate(client, app):
    r = post(client, ["m1", "m2"], captions={"m1": "Nike AF1 size 42 Rs 4500"})
    assert r.status_code == 202, r.text
    body = r.json()
    assert body["status"] == "accepted" and body["rejected"] == []
    assert app.state.woke == [1]
    r2 = post(client, ["m1", "m2"])
    assert r2.status_code == 200 and r2.json() == {"status": "duplicate", "post_id": body["post_id"]}


def test_stores_refs_not_raw_ids_and_strips_media(client, app, db_url, tmp_path):
    post(client, ["m1"])
    with psycopg.connect(db_url) as c:
        chat, sender, jid = c.execute("SELECT chat_ref, sender_ref, sender_jid FROM posts").fetchone()
        sha = c.execute("SELECT sha256 FROM images").fetchone()[0]
    assert len(chat) == 32 and "123" not in chat and len(sender) == 32
    assert jid == "923001234567@s.whatsapp.net"  # local DB only (Q6)
    assert app.state.media_store.get(sha)[:3] == b"\xff\xd8\xff"


def test_duplicate_never_parses_body(client, monkeypatch):
    assert post(client, ["m1"]).status_code == 202

    async def boom(self, **kw):
        raise AssertionError("form parsed for a duplicate")

    monkeypatch.setattr(Request, "form", boom)
    assert post(client, ["m1"]).status_code == 200


@pytest.mark.parametrize("headers,status", [
    ({}, 401),
    ({"X-Ingest-Token": "wrong"}, 401),
])
def test_token_required(client, headers, status):
    _, data, files = album(["m1"])
    assert client.post("/ingest", headers=headers, data=data, files=files).status_code == status


def test_wrong_host_421(app):
    with TestClient(app, base_url="http://evil.example:8000") as c:
        h, data, files = album(["m1"])
        assert c.post("/ingest", headers=h, data=data, files=files).status_code == 421


def test_admin_queue_needs_token(client):
    assert client.get("/admin/queue").status_code == 401
    post(client, ["m1"])
    r = client.get("/admin/queue", headers={"X-Ingest-Token": TOKEN})
    assert r.json() == {"received": 1}


def test_other_mutations_need_custom_header(client):
    assert client.post("/api/whatever").status_code == 403
    assert client.post("/api/whatever", headers={"X-ThriftRadar": "1"}).status_code == 404


def test_bad_idempotency_key(client):
    h, data, files = album(["m1"])
    h["Idempotency-Key"] = "nothex"
    assert client.post("/ingest", headers=h, data=data, files=files).json()["detail"] == "bad_idempotency_key"


def test_key_mismatch(client):
    h, data, files = album(["m1"])
    h["Idempotency-Key"] = idempotency_key("whatsapp", "123@g.us", ["other"])
    r = client.post("/ingest", headers=h, data=data, files=files)
    assert r.status_code == 400 and r.json()["detail"] == "key_mismatch"


def test_superset_ingests_only_new_messages(client, db_url):
    post(client, ["m1", "m2"])
    r = post(client, ["m1", "m2", "m3"])
    assert r.status_code == 202
    with psycopg.connect(db_url) as c:
        rows = c.execute("SELECT post_id, msg_key FROM post_messages ORDER BY post_id, seq").fetchall()
    assert [k for pid, k in rows if pid == r.json()["post_id"]] == ["m3"]


def test_all_known_messages_is_duplicate(client):
    post(client, ["m1", "m2"])
    r = post(client, ["m2", "m1"], chat="123@g.us")  # same set, same key → whole-post dup
    assert r.json()["status"] == "duplicate"
    r = post(client, ["m1"])  # subset → different key, but every message known
    assert r.status_code == 200 and r.json() == {"status": "duplicate", "post_id": None}


def test_corrupt_image_among_good(client, db_url):
    r = post(client, ["m1", "m2"], files={"m2": b"\xff\xd8\xff" + b"garbage" * 10})
    assert r.status_code == 202
    assert r.json()["rejected"] == [{"key": "m2", "reason": "corrupt"}]
    with psycopg.connect(db_url) as c:
        row = c.execute("SELECT missing_media, reject_reason, image_id FROM post_messages WHERE msg_key='m2'").fetchone()
    assert row == (True, "corrupt", None)


def test_all_corrupt_422(client):
    r = post(client, ["m1"], files={"m1": b"not an image"})
    assert r.status_code == 422 and r.json()["detail"] == "no_valid_images"


def test_text_message_kept_with_images(client, db_url):
    assert post(client, ["m1", "t1"], text_keys=("t1",)).status_code == 202
    with psycopg.connect(db_url) as c:
        assert c.execute("SELECT kind FROM post_messages ORDER BY seq").fetchall() == [("image",), ("text",)]


def test_invalid_meta_does_not_echo_captions(client):
    h, data, files = album(["m1"])
    meta = json.loads(data["meta"])
    meta["messages"][0]["caption"] = "SECRET-CAPTION " + "x" * 5000
    r = client.post("/ingest", headers=h, data={"meta": json.dumps(meta)}, files=files)
    assert r.status_code == 422 and "SECRET-CAPTION" not in r.text


def test_naive_timestamp_rejected(client):
    h, data, files = album(["m1"])
    meta = json.loads(data["meta"])
    meta["messages"][0]["sent_at"] = "2026-09-01T12:00:00"
    assert client.post("/ingest", headers=h, data={"meta": json.dumps(meta)}, files=files).status_code == 422


def test_extra_file_rejected(client):
    h, data, files = album(["m1"])
    files.append(("extra", ("x.jpg", encode(pattern(1)), "image/jpeg")))
    r = client.post("/ingest", headers=h, data=data, files=files)
    assert r.status_code == 422 and r.json()["detail"] == "file_fields_mismatch"


def test_too_many_files_413(client):
    keys = [f"m{i}" for i in range(31)]
    h, data, files = album(keys)
    small = encode(pattern(1, (32, 32)))
    files = [(f, (n, small, t)) for f, (n, _, t) in files]
    meta = json.loads(data["meta"])
    meta["messages"] = meta["messages"][:30]  # meta itself is capped at 60 messages; the file count trips first
    r = client.post("/ingest", headers=h, data={"meta": json.dumps(meta)}, files=files)
    assert r.status_code == 413


def test_oversized_content_length_413(client):
    h, _, _ = album(["m1"])
    h["Content-Length"] = str(200 * 1024 * 1024)
    h["Content-Type"] = "multipart/form-data; boundary=x"
    assert client.post("/ingest", headers=h, content=b"").status_code == 413


def test_create_app_refuses_demo_mode_on_local_db(db_url, tmp_path):
    with pytest.raises(StartupError, match="role"):
        create_app(_settings(db_url, tmp_path, DEMO_MODE="1", session_secret="s"))


def test_create_app_refuses_local_mode_on_demo_db(demo_db_url, tmp_path):
    with pytest.raises(StartupError, match="role"):
        create_app(_settings(demo_db_url, tmp_path))


@pytest.mark.parametrize("method", ["GET", "POST", "PUT", "DELETE"])
@pytest.mark.parametrize("path", ["/ingest", "/admin/queue", "/listener/heartbeat", "/api/listener/status"])
def test_demo_mode_404s_local_routes(demo_db_url, tmp_path, method, path):
    app = create_app(_settings(demo_db_url, tmp_path, DEMO_MODE="1", session_secret="s"))
    with TestClient(app, base_url=BASE) as c:
        r = c.request(method, path, headers={"X-Ingest-Token": TOKEN})
    assert r.status_code == 404 and r.json() == {"detail": "Not Found"}


def test_healthz_and_config(client):
    assert client.get("/healthz").json()["demo"] is False
    assert client.get("/api/config").json() == {"demo": False, "features": {"whatsapp": True}}
    assert client.get("/healthz").headers["x-content-type-options"] == "nosniff"
