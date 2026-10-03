import threading
from datetime import datetime, timezone

import psycopg
import pytest

from app.migrate import WrongRole, apply_migrations, migration_files

EXPECTED_TABLES = {
    "db_meta", "posts", "post_messages", "images", "listings", "listing_sightings",
    "vlm_calls", "wishlists", "matches", "listener_status", "media_blobs", "schema_migrations",
}


def _post_values(source: str, key: str, sender_jid: str | None = None):
    now = datetime.now(timezone.utc)
    return (source, key, "c" * 32, "s" * 32, sender_jid, now, now)


INSERT_POST = (
    "INSERT INTO posts (source, idempotency_key, chat_ref, sender_ref, sender_jid,"
    " first_msg_at, last_msg_at) VALUES (%s, %s, %s, %s, %s, %s, %s)"
)


def test_applies_cleanly_and_is_idempotent(fresh_db_url):
    first = apply_migrations(fresh_db_url)
    assert first == [name for name, _ in migration_files(demo=False)]
    assert apply_migrations(fresh_db_url) == []
    with psycopg.connect(fresh_db_url) as conn:
        tables = {r[0] for r in conn.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public'")}
        assert EXPECTED_TABLES <= tables
        meta = dict(conn.execute("SELECT key, value FROM db_meta").fetchall())
        assert meta["role"] == "local"
        assert meta["schema_version"] == first[-1]


def test_concurrent_runners_apply_once(fresh_db_url):
    results, errors = [], []

    def run():
        try:
            results.append(apply_migrations(fresh_db_url))
        except Exception as e:  # pragma: no cover - surfaced by the assert below
            errors.append(e)

    threads = [threading.Thread(target=run) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert sorted(len(r) for r in results) == [0, 0, 0, len(migration_files(demo=False))]


def test_local_db_accepts_whatsapp_posts(db_url):
    with psycopg.connect(db_url) as conn:
        conn.execute(INSERT_POST, _post_values("whatsapp", "k-local-1", "923000000000@s.whatsapp.net"))
        conn.rollback()


def test_unique_idempotency_key(db_url):
    with psycopg.connect(db_url) as conn:
        conn.execute(INSERT_POST, _post_values("whatsapp", "k-dup"))
        with pytest.raises(psycopg.errors.UniqueViolation):
            conn.execute(INSERT_POST, _post_values("whatsapp", "k-dup"))
        conn.rollback()


def test_processing_requires_claim(db_url):
    with psycopg.connect(db_url) as conn:
        conn.execute(INSERT_POST, _post_values("whatsapp", "k-claim"))
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute("UPDATE posts SET status = 'processing' WHERE idempotency_key = 'k-claim'")
        conn.rollback()


def test_demo_role_and_lockdown(demo_db_url):
    with psycopg.connect(demo_db_url) as conn:
        assert conn.execute("SELECT value FROM db_meta WHERE key = 'role'").fetchone()[0] == "demo"
        conn.execute(INSERT_POST, _post_values("demo_seed", "k-seed"))
        conn.rollback()


@pytest.mark.parametrize("source", ["whatsapp", "chat_export"])
def test_demo_rejects_real_sources(demo_db_url, source):
    with psycopg.connect(demo_db_url) as conn:
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(INSERT_POST, _post_values(source, f"k-{source}"))


def test_demo_rejects_sender_jid(demo_db_url):
    with psycopg.connect(demo_db_url) as conn:
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(INSERT_POST, _post_values("demo_upload", "k-jid", "923000000000@s.whatsapp.net"))


def test_local_run_refuses_demo_db(demo_db_url):
    with pytest.raises(WrongRole):
        apply_migrations(demo_db_url, demo=False)
