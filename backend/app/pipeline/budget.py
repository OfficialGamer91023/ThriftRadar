"""VLM spend controls: the vlm_calls ledger, caps, circuit breaker and rate limiter. Spec: DESIGN.md §4.5
`reserve_vlm_call`, `finish_vlm_call`, `CircuitBreaker`; retry table in §3.1."""

import logging
import threading
import time
from datetime import UTC, datetime, timedelta

import psycopg
from psycopg.types.json import Jsonb

log = logging.getLogger(__name__)

DAILY_CAP = "DAILY_CAP"
POST_CAP = "POST_CAP"
ADVISORY_KEY = 72430001
# Statuses that may have been billed, so they count toward the per-post attempt cap.
BILLABLE = ("reserved", "ok", "bad_json", "timeout")


def utc_today() -> datetime:
    return datetime.now(UTC)


def next_utc_midnight(now: datetime | None = None) -> datetime:
    now = now or datetime.now(UTC)
    return datetime(now.year, now.month, now.day, tzinfo=UTC) + timedelta(days=1)


def _spent_today(conn: psycopg.Connection) -> int:
    """Calls that may have been billed today (a 429 or 401 was not, so it doesn't use up the day)."""
    return conn.execute("SELECT count(*) FROM vlm_calls WHERE day = (now() AT TIME ZONE 'utc')::date "
                        "AND status = ANY(%s)", (list(BILLABLE),)).fetchone()[0]


def budget_available(conn: psycopg.Connection, daily_cap: int) -> bool:
    return _spent_today(conn) < daily_cap


def reserve_vlm_call(conn: psycopg.Connection, *, post_id: int, segment_idx: int, prompt_version: str,
                     provider: str, model: str, daily_cap: int, max_attempts: int) -> int | str:
    """One transaction under an advisory lock, so concurrent workers can't both take the last unit of budget."""
    with conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (ADVISORY_KEY,))
        if _spent_today(conn) >= daily_cap:
            return DAILY_CAP
        used = conn.execute(
            "SELECT count(*) FROM vlm_calls WHERE post_id = %s AND segment_idx = %s AND status = ANY(%s)",
            (post_id, segment_idx, list(BILLABLE))).fetchone()[0]
        if used >= max_attempts:
            return POST_CAP
        return conn.execute(
            """INSERT INTO vlm_calls (post_id, segment_idx, attempt, provider, model, prompt_version, day)
               VALUES (%(p)s, %(s)s,
                       (SELECT coalesce(max(attempt), 0) + 1 FROM vlm_calls
                        WHERE post_id = %(p)s AND segment_idx = %(s)s AND prompt_version = %(pv)s),
                       %(prov)s, %(model)s, %(pv)s, (now() AT TIME ZONE 'utc')::date)
               RETURNING id""",
            {"p": post_id, "s": segment_idx, "pv": prompt_version, "prov": provider, "model": model}).fetchone()[0]


def estimate_cost(input_tokens: int | None, output_tokens: int | None, *, floor_input: int,
                  price_in_per_m: float, price_out_per_m: float) -> float:
    """Usage-based cost, with input floored at the request's own estimate: some providers under-report image tokens."""
    tin = max(input_tokens or 0, floor_input)
    return round(tin * price_in_per_m / 1e6 + (output_tokens or 0) * price_out_per_m / 1e6, 6)


def finish_vlm_call(conn: psycopg.Connection, call_id: int, *, status: str, http_status: int | None,
                    input_tokens: int | None, output_tokens: int | None, cost_usd: float | None,
                    latency_ms: int | None, response: dict | None, raw_excerpt: str | None) -> bool:
    """Idempotent: only a `reserved` row is finished."""
    with conn.transaction():
        n = conn.execute(
            """UPDATE vlm_calls SET status = %s, http_status = %s, input_tokens = %s, output_tokens = %s,
                      cost_usd = %s, latency_ms = %s, response = %s, raw_excerpt = %s, finished_at = now()
               WHERE id = %s AND status = 'reserved'""",
            (status, http_status, input_tokens, output_tokens, cost_usd, latency_ms,
             Jsonb(response) if response is not None else None, (raw_excerpt or None) and raw_excerpt[:2048],
             call_id)).rowcount
    return n == 1


def cached_response(conn: psycopg.Connection, post_id: int, segment_idx: int, prompt_version: str) -> dict | None:
    row = conn.execute(
        """SELECT response FROM vlm_calls WHERE post_id = %s AND segment_idx = %s AND prompt_version = %s
             AND status = 'ok' AND response IS NOT NULL ORDER BY id DESC LIMIT 1""",
        (post_id, segment_idx, prompt_version)).fetchone()
    return row[0] if row else None


class CircuitBreaker:
    """In memory. Open for a cooldown after 401/402/403; then half-open: one call through, success closes it."""

    def __init__(self, clock=time.monotonic):
        self._clock = clock
        self._lock = threading.Lock()
        self._open_until = 0.0
        self._half_open_taken = False
        self.opened = False

    def open(self, seconds: float, reason: str = "") -> None:
        with self._lock:
            self._open_until = self._clock() + seconds
            self._half_open_taken = False
            self.opened = True
        log.error("vlm breaker open %ss reason=%s", int(seconds), reason)

    def allow(self) -> bool:
        """True if a call may go out now (closed, or the single half-open probe)."""
        with self._lock:
            if not self.opened:
                return True
            if self._clock() < self._open_until or self._half_open_taken:
                return False
            self._half_open_taken = True
            return True

    def is_open(self) -> bool:
        with self._lock:
            return self.opened and (self._clock() < self._open_until or self._half_open_taken)

    def success(self) -> None:
        with self._lock:
            self.opened = False
            self._half_open_taken = False

    def failure(self, seconds: float, reason: str = "") -> None:
        self.open(seconds, reason)

    def reopen_at(self) -> datetime:
        with self._lock:
            left = max(0.0, self._open_until - self._clock())
        return datetime.now(UTC) + timedelta(seconds=left or 60)


class RateLimiter:
    """Token bucket: at most `rpm` calls per minute across all VLM threads."""

    def __init__(self, rpm: int, clock=time.monotonic, sleep=time.sleep):
        self.capacity = max(1, rpm)
        self.rate = self.capacity / 60.0
        self.tokens = float(self.capacity)
        self.last = clock()
        self._clock, self._sleep = clock, sleep
        self._lock = threading.Lock()

    def acquire(self) -> None:
        while True:
            with self._lock:
                now = self._clock()
                self.tokens = min(self.capacity, self.tokens + (now - self.last) * self.rate)
                self.last = now
                if self.tokens >= 1:
                    self.tokens -= 1
                    return
                wait = (1 - self.tokens) / self.rate
            self._sleep(wait)
