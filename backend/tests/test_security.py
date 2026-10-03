import asyncio
import logging

import pytest
from starlette.requests import Request

from app.main import RedactFilter
from app.security import BodySizeLimitMiddleware, client_ip


def _request(xff: str | None, host="10.0.0.9") -> Request:
    headers = [(b"x-forwarded-for", xff.encode())] if xff is not None else []
    return Request({"type": "http", "headers": headers, "client": (host, 1234)})


@pytest.mark.parametrize("xff,hops,expected", [
    ("1.2.3.4", 1, "1.2.3.4"),
    ("6.6.6.6, 1.2.3.4", 1, "1.2.3.4"),
    ("", 1, "10.0.0.9"),
    (None, 1, "10.0.0.9"),
    ("garbage", 1, "unknown"),
    ("6.6.6.6, 1.2.3.4, 10.1.1.1", 2, "1.2.3.4"),
    ("1.2.3.4", 0, "10.0.0.9"),
])
def test_client_ip(xff, hops, expected):
    assert client_ip(_request(xff), hops) == expected


def _run(mw, scope, chunks):
    sent, received = [], []

    async def receive():
        received.append(1)
        body = chunks.pop(0) if chunks else b""
        return {"type": "http.request", "body": body, "more_body": bool(chunks)}

    async def send(message):
        sent.append(message)

    asyncio.run(mw(scope, receive, send))
    return sent, received


async def _reads_body(scope, receive, send):
    while True:
        m = await receive()
        if not m.get("more_body"):
            break
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"ok"})


def _scope(path, headers=()):
    return {"type": "http", "path": path, "method": "POST", "headers": list(headers)}


def test_declared_length_rejected_without_reading():
    mw = BodySizeLimitMiddleware(_reads_body, {"/ingest": 100})
    sent, received = _run(mw, _scope("/ingest", [(b"content-length", b"101")]), [b"x" * 101])
    assert sent[0]["status"] == 413 and received == []


def test_chunked_body_over_limit():
    mw = BodySizeLimitMiddleware(_reads_body, {"/ingest": 100})
    sent, _ = _run(mw, _scope("/ingest"), [b"x" * 60, b"x" * 60])
    assert sent[0]["status"] == 413


def test_under_limit_passes_and_default_applies():
    mw = BodySizeLimitMiddleware(_reads_body, {"/ingest": 100}, default=10)
    assert _run(mw, _scope("/ingest"), [b"x" * 90])[0][0]["status"] == 200
    assert _run(mw, _scope("/api/search"), [b"x" * 11])[0][0]["status"] == 413


def test_redact_filter_masks_phone_numbers():
    record = logging.LogRecord("t", logging.INFO, __file__, 1, "sender %s sent", ("923001234567",), None)
    RedactFilter().filter(record)
    assert "923001234567" not in record.getMessage()
    assert record.getMessage().startswith("sender 92")
