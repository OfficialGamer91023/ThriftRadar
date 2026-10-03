import threading
from unittest import mock

import psycopg
import pytest

from app import db
from app.images import normalize_image
from app.media_store import (BundledStore, CompositeStore, LocalDirStore, MediaMissing, PgBlobStore,
                             ReadOnlyStore)
from tests.imgutil import encode, pattern


def test_tx_commits_and_rolls_back(db_url):
    pool = db.make_pool(db_url, 2)
    try:
        with db.tx(pool) as conn:
            conn.execute("INSERT INTO db_meta (key, value) VALUES ('t_commit', '1')")
        with pytest.raises(RuntimeError):
            with db.tx(pool) as conn:
                conn.execute("INSERT INTO db_meta (key, value) VALUES ('t_rollback', '1')")
                raise RuntimeError
        with db.tx(pool) as conn:
            keys = {r[0] for r in conn.execute("SELECT key FROM db_meta WHERE key LIKE 't\\_%'")}
            conn.execute("DELETE FROM db_meta WHERE key LIKE 't\\_%'")
        assert keys == {"t_commit"}
    finally:
        pool.close()


def test_pool_registers_pgvector(db_url):
    pool = db.make_pool(db_url, 1)
    try:
        with db.tx(pool) as conn:
            v = conn.execute("SELECT '[1,2,3]'::vector").fetchone()[0]
        assert v.to_list() == [1, 2, 3]
    finally:
        pool.close()


def test_get_conn_retries_then_succeeds():
    pool = mock.Mock()
    conn = object()
    pool.getconn.side_effect = [psycopg.OperationalError("resuming"), psycopg.OperationalError("resuming"), conn]
    with db.get_conn(pool, sleeps=(0, 0, 0)) as got:
        assert got is conn
    pool.putconn.assert_called_once_with(conn)


def test_get_conn_gives_up():
    pool = mock.Mock()
    pool.getconn.side_effect = psycopg.OperationalError("down")
    with pytest.raises(psycopg.OperationalError):
        with db.get_conn(pool, sleeps=(0, 0)):
            pass
    assert pool.getconn.call_count == 3


def test_local_dir_store_roundtrip_and_missing(tmp_path):
    store = LocalDirStore(tmp_path)
    sha = bytes(range(32))
    with pytest.raises(MediaMissing):
        store.get(sha)
    store.put(sha, b"abc")
    store.put(sha, b"ignored: content-addressed, first write wins")
    assert store.get(sha) == b"abc"
    assert store.path(sha).parent.name == sha.hex()[:2]
    assert not list(tmp_path.rglob(".tmp-*"))


def test_local_dir_store_concurrent_put(tmp_path):
    store = LocalDirStore(tmp_path)
    sha = b"\x01" * 32
    data = b"x" * 200_000
    errors = []

    def put():
        try:
            store.put(sha, data)
        except Exception as e:  # pragma: no cover
            errors.append(e)

    threads = [threading.Thread(target=put) for _ in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert store.get(sha) == data
    assert not list(tmp_path.rglob(".tmp-*"))


def test_bundled_store(tmp_path):
    (tmp_path / "a.png").write_bytes(encode(pattern(1), "PNG"))
    store = BundledStore(tmp_path, ["a.png"])
    sha = normalize_image(encode(pattern(1), "PNG"), 2048).sha256
    assert sha in store
    assert store.get(sha)[:3] == b"\xff\xd8\xff"
    with pytest.raises(ReadOnlyStore):
        store.put(sha, b"")


def test_pg_blob_store(db_url):
    pool = db.make_pool(db_url, 2)
    try:
        store = PgBlobStore(pool)
        sha = b"\x02" * 32
        store.put(sha, b"blob")
        store.put(sha, b"blob")
        assert store.get(sha) == b"blob"
        with pytest.raises(MediaMissing):
            store.get(b"\x03" * 32)
    finally:
        pool.close()


def test_composite_store(tmp_path):
    bundled_dir = tmp_path / "seed"
    bundled_dir.mkdir()
    (bundled_dir / "a.png").write_bytes(encode(pattern(1), "PNG"))
    bundled = BundledStore(bundled_dir, ["a.png"])
    local = LocalDirStore(tmp_path / "media")
    store = CompositeStore([bundled, local])
    seed_sha = next(iter(bundled._blobs))
    assert store.get(seed_sha)
    store.put(b"\x04" * 32, b"up")
    assert local.get(b"\x04" * 32) == b"up"
    with pytest.raises(MediaMissing):
        store.get(b"\x05" * 32)
