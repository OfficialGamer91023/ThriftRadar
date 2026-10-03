"""Connection pool and transactions. Spec: DESIGN.md §4.3 `db.tx` / `db.get_conn`."""

import logging
import time
from collections.abc import Iterator
from contextlib import contextmanager

import psycopg
from pgvector.psycopg import register_vector
from psycopg_pool import ConnectionPool

log = logging.getLogger(__name__)

ACQUIRE_RETRY_SLEEPS = (1, 2, 4)


def _configure(conn: psycopg.Connection) -> None:
    register_vector(conn)
    conn.commit()  # register_vector reads pg_type, which opens a transaction


def make_pool(conninfo: str, max_size: int) -> ConnectionPool:
    return ConnectionPool(
        conninfo,
        min_size=0,
        max_size=max_size,
        max_idle=60,
        timeout=15,
        kwargs={"prepare_threshold": None, "connect_timeout": 10, "autocommit": False},
        configure=_configure,
        check=ConnectionPool.check_connection,
        open=True,
    )


@contextmanager
def get_conn(pool: ConnectionPool, sleeps: tuple[float, ...] = ACQUIRE_RETRY_SLEEPS) -> Iterator[psycopg.Connection]:
    """Borrow a connection, retrying acquisition so a resuming Neon compute doesn't fail the request."""
    for i in range(len(sleeps) + 1):
        try:
            conn = pool.getconn()
            break
        except psycopg.OperationalError as e:  # includes psycopg_pool.PoolTimeout
            if i == len(sleeps):
                raise
            log.warning("db acquire failed (%s), retry %d in %ss", type(e).__name__, i + 1, sleeps[i])
            time.sleep(sleeps[i])
    try:
        yield conn
    finally:
        pool.putconn(conn)  # the pool rolls back anything left open


@contextmanager
def tx(pool: ConnectionPool) -> Iterator[psycopg.Connection]:
    """BEGIN … COMMIT, ROLLBACK on exception."""
    with get_conn(pool) as conn:
        with conn.transaction():
            yield conn
