"""Listener heartbeat and status (local mode). Spec: DESIGN.md §4.3."""

import psycopg
import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.settings import Settings

BASE = "http://127.0.0.1:8000"
TOKEN = "t" * 32


class RecordingNotifier:
    def __init__(self):
        self.sent = []

    def send(self, title, body):
        self.sent.append((title, body))
        return True


@pytest.fixture
def client(db_url, tmp_path):
    with psycopg.connect(db_url) as c:
        c.execute("DELETE FROM listener_status")
        c.commit()
    app = create_app(Settings(_env_file=None, database_url=db_url, ingest_token=TOKEN, sender_hmac_key="k",
                              media_dir=str(tmp_path), vlm_provider="off", load_models=False))
    app.state.notifier = RecordingNotifier()
    with TestClient(app, base_url=BASE) as c:
        yield c, app.state.notifier


def beat(c, **body):
    return c.post("/listener/heartbeat", json={"state": "open"} | body, headers={"X-Ingest-Token": TOKEN})


def test_heartbeat_needs_the_token(client):
    c, _ = client
    assert c.post("/listener/heartbeat", json={"state": "open"}).status_code == 401


def test_status_before_and_after_a_heartbeat(client):
    c, _ = client
    assert c.get("/api/listener/status").json() == {"state": "never_seen", "stale": True}
    assert beat(c, state="awaiting_qr", spool_open=1, spool_ready=2, drop_counts={"other_chat": 5},
                version="0.1.0").status_code == 200
    s = c.get("/api/listener/status").json()
    assert s["state"] == "awaiting_qr" and s["stale"] is False and s["spool_pending"] == 3
    assert s["counts"] == {"other_chat": 5}
    assert beat(c, state="nonsense").status_code == 422


def test_one_notification_per_bad_transition(client):
    c, notifier = client
    beat(c, state="open")
    beat(c, state="logged_out")
    beat(c, state="logged_out")
    assert len(notifier.sent) == 1 and "logged out" in notifier.sent[0][1]
    beat(c, state="open")  # recovered: a later logout notifies again
    beat(c, state="logged_out")
    assert len(notifier.sent) == 2


def test_listener_routes_are_404_in_demo(demo_db_url, tmp_path):
    app = create_app(Settings(_env_file=None, DEMO_MODE="1", database_url=demo_db_url, session_secret="s",
                              sender_hmac_key="k", vlm_provider="off", load_models=False,
                              seed_dir=str(tmp_path)))
    with TestClient(app, base_url="https://demo.test") as c:
        assert c.post("/listener/heartbeat", json={"state": "open"}).status_code == 404
        assert c.get("/api/listener/status").status_code == 404


def test_a_deliberate_shutdown_does_not_notify(client):
    c, notifier = client
    beat(c, state="open")
    beat(c, state="stopped", detail="shutdown")
    assert notifier.sent == []
    beat(c, state="stopped", detail="reconnect storm")
    assert len(notifier.sent) == 1
