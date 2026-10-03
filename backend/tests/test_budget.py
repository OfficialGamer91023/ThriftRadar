"""VLM ledger, caps, breaker, rate limiter. Spec: DESIGN.md §4.5 `reserve_vlm_call`; tests §6.3."""

import threading
from datetime import datetime, timezone

import psycopg
import pytest

from app.pipeline.budget import (DAILY_CAP, POST_CAP, CircuitBreaker, RateLimiter, budget_available,
                                 finish_vlm_call, next_utc_midnight, reserve_vlm_call)
from tests.fakes import make_post


@pytest.fixture
def conn(db_url):
    with psycopg.connect(db_url, autocommit=True) as c:
        c.execute("TRUNCATE posts RESTART IDENTITY CASCADE")
        yield c


def reserve(conn, post_id, seg=0, cap=100, max_attempts=3):
    return reserve_vlm_call(conn, post_id=post_id, segment_idx=seg, prompt_version="v1", provider="fake",
                            model="m", daily_cap=cap, max_attempts=max_attempts)


def finish(conn, call_id, status, http=None):
    return finish_vlm_call(conn, call_id, status=status, http_status=http, input_tokens=None, output_tokens=None,
                           cost_usd=0, latency_ms=1, response=None, raw_excerpt=None)


def test_daily_cap(conn):
    pids = [make_post(conn, None, f"p{i}", [i]) for i in range(4)]
    ids = [reserve(conn, pids[i], cap=3) for i in range(3)]
    assert all(isinstance(i, int) for i in ids)
    assert reserve(conn, pids[3], cap=3) == DAILY_CAP
    assert not budget_available(conn, 3)


def test_post_cap_counts_crashed_reservations_but_not_429(conn):
    pid = make_post(conn, None, "p", [1])
    first = reserve(conn, pid)  # left 'reserved', as after a crash: still counts
    second = reserve(conn, pid)
    finish(conn, second, "http_error", 429)  # not billed: doesn't count
    third = reserve(conn, pid)
    finish(conn, third, "timeout")
    assert reserve(conn, pid) != POST_CAP  # reserved + timeout = 2 of 3
    assert reserve(conn, pid) == POST_CAP
    attempts = [r[0] for r in conn.execute("SELECT attempt FROM vlm_calls ORDER BY id")]
    assert attempts == [1, 2, 3, 4] and first == 1


def test_finish_is_idempotent(conn):
    pid = make_post(conn, None, "p", [1])
    cid = reserve(conn, pid)
    assert finish(conn, cid, "ok") and not finish(conn, cid, "timeout")
    assert conn.execute("SELECT status FROM vlm_calls").fetchone() == ("ok",)


def test_concurrent_reservations_respect_the_cap(conn, db_url):
    pids = [make_post(conn, None, f"p{i}", [i]) for i in range(20)]
    got, barrier = [], threading.Barrier(20)

    def worker(pid):
        with psycopg.connect(db_url, autocommit=True) as c:
            barrier.wait()
            got.append(reserve(c, pid, cap=5))

    threads = [threading.Thread(target=worker, args=(p,)) for p in pids]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sum(isinstance(g, int) for g in got) == 5 and got.count(DAILY_CAP) == 15
    assert conn.execute("SELECT count(*) FROM vlm_calls").fetchone()[0] == 5


def test_breaker_half_open():
    now = [0.0]
    b = CircuitBreaker(clock=lambda: now[0])
    assert b.allow() and not b.is_open()
    b.open(900)
    assert b.is_open() and not b.allow()
    now[0] = 901
    assert b.allow()  # the one half-open probe
    assert not b.allow()  # nobody else until it resolves
    b.success()
    assert b.allow() and not b.is_open()


def test_rate_limiter_waits_when_empty():
    now, slept = [0.0], []
    rl = RateLimiter(2, clock=lambda: now[0], sleep=lambda s: (slept.append(s), now.__setitem__(0, now[0] + s)))
    rl.acquire()
    rl.acquire()
    rl.acquire()
    assert slept and slept[0] == pytest.approx(30, abs=0.01)  # 2 per minute: the third waits 30 s


def test_next_utc_midnight():
    assert next_utc_midnight(datetime(2026, 10, 3, 23, 59, tzinfo=timezone.utc)) == \
        datetime(2026, 10, 4, tzinfo=timezone.utc)
