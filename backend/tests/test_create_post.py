import threading
from datetime import datetime, timedelta, timezone

import psycopg
import pytest

from app.ingest_service import ImageInput, MsgInput, OverlapConflict, PostInput, create_post

T0 = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)


def _img(n: int) -> ImageInput:
    return ImageInput(sha256=bytes([n]) * 32, phash=n, width=10, height=10, bytes=100)


def _post(key: str, msg_keys: list[str], chat="chat-a") -> PostInput:
    msgs = [MsgInput(k, "image", T0 + timedelta(seconds=i), "Nike size 42" if i == 0 else None, _img(i))
            for i, k in enumerate(msg_keys)]
    return PostInput(source="whatsapp", idempotency_key=key, chat_ref=chat, sender_ref="s" * 32, messages=msgs)


@pytest.fixture
def conn(db_url):
    with psycopg.connect(db_url) as c:
        c.execute("TRUNCATE posts RESTART IDENTITY CASCADE")
        c.commit()
        yield c


def test_created_then_duplicate(conn):
    r1 = create_post(conn, _post("k1", ["m1", "m2"]))
    assert r1.status == "created" and len(r1.image_ids) == 2
    r2 = create_post(conn, _post("k1", ["m1", "m2"]))
    assert r2.status == "duplicate" and r2.post_id == r1.post_id
    row = conn.execute("SELECT status, priority, first_msg_at, last_msg_at FROM posts").fetchone()
    assert row == ("received", 0, T0, T0 + timedelta(seconds=1))
    links = conn.execute("SELECT count(*) FROM post_messages WHERE image_id IS NOT NULL").fetchone()[0]
    assert links == 2


def test_overlap_conflict_rolls_back(conn):
    create_post(conn, _post("k1", ["m1", "m2"]))
    with pytest.raises(OverlapConflict):
        create_post(conn, _post("k2", ["m2", "m3"]))
    assert conn.execute("SELECT count(*) FROM posts").fetchone()[0] == 1
    assert conn.execute("SELECT count(*) FROM images").fetchone()[0] == 2


def test_same_msg_key_other_chat_is_fine(conn):
    create_post(conn, _post("k1", ["m1"], chat="chat-a"))
    assert create_post(conn, _post("k2", ["m1"], chat="chat-b")).status == "created"


def test_concurrent_same_key(db_url, conn):
    results, errors = [], []
    barrier = threading.Barrier(10)

    def worker():
        try:
            with psycopg.connect(db_url) as c:
                barrier.wait()
                results.append(create_post(c, _post("k-race", ["r1", "r2"])).status)
                c.commit()
        except Exception as e:  # pragma: no cover
            errors.append(e)

    threads = [threading.Thread(target=worker) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert sorted(results) == ["created"] + ["duplicate"] * 9
    assert conn.execute("SELECT count(*) FROM posts").fetchone()[0] == 1
