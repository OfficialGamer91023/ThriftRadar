"""requeue.py. Spec: DESIGN.md §4.8 `requeue.py`; tests §6.4."""

import psycopg
import pytest

from app import db
from app.media_store import LocalDirStore
from app.settings import Settings
from app.worker import LocalCtx, drain
from scripts.requeue import Refused, requeue_failed, requeue_local
from tests.fakes import FakeModels, make_post


@pytest.fixture
def conn(db_url):
    with psycopg.connect(db_url) as c:
        c.execute("TRUNCATE posts RESTART IDENTITY CASCADE")
        c.commit()
        yield c


def counts(conn):
    return conn.execute("""SELECT (SELECT count(*) FROM listings), (SELECT count(*) FROM listing_sightings),
                                  (SELECT count(*) FROM posts WHERE status = 'done'),
                                  (SELECT count(*) FROM images WHERE embedding IS NOT NULL)""").fetchone()


def test_local_requeue_dry_run_then_apply_then_redrain(conn, db_url, tmp_path):
    store = LocalDirStore(tmp_path)
    make_post(conn, store, "a", [1, 2], caption="Nike size 42 Rs 4000")
    make_post(conn, store, "b", [1, 2])  # a repost of a
    pool = db.make_pool(db_url, 2)
    try:
        ctx = LocalCtx(pool, Settings(ingest_token="t", sender_hmac_key="k"), FakeModels(), store)
        drain(ctx)
        before = counts(conn)
        assert before == (1, 2, 2, 2)
        assert requeue_local(conn, "chat_export", apply=False) == {"posts": 2, "listings": 1, "sightings": 2}
        conn.commit()
        assert counts(conn) == before
        requeue_local(conn, "chat_export", apply=True)
        conn.commit()
        assert counts(conn) == (0, 0, 0, 0)
        assert conn.execute("SELECT DISTINCT status, attempts, outcome FROM posts").fetchall() == [("received", 0, None)]
        drain(ctx)
        assert counts(conn) == before
    finally:
        pool.close()


def test_local_requeue_refuses_to_orphan_other_sources(conn, db_url, tmp_path):
    store = LocalDirStore(tmp_path)
    make_post(conn, store, "a", [1], caption="Nike size 42 Rs 4000")
    make_post(conn, store, "live", [1], source="whatsapp")
    pool = db.make_pool(db_url, 2)
    try:
        # process the export first so the live post becomes its repost
        conn.execute("UPDATE posts SET priority = 30 WHERE source = 'whatsapp'")
        conn.commit()
        drain(LocalCtx(pool, Settings(ingest_token="t", sender_hmac_key="k"), FakeModels(), store))
    finally:
        pool.close()
    with pytest.raises(Refused):
        requeue_local(conn, "chat_export", apply=True)


def test_failed_requeue(conn, tmp_path):
    make_post(conn, None, "a", [1])
    conn.execute("UPDATE posts SET status = 'failed', attempts = 3, last_error = 'media_missing'")
    assert requeue_failed(conn, "whatsapp", None, apply=True) == 0
    assert requeue_failed(conn, None, None, apply=True) == 1
    assert conn.execute("SELECT status, attempts, last_error FROM posts").fetchone() == ("received", 0, None)
