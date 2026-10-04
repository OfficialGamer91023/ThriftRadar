"""Listener heartbeat and status. Spec: DESIGN.md §4.3 `POST /listener/heartbeat`, `GET /api/listener/status`.
Local mode only (not mounted in demo). The heartbeat path needs X-Ingest-Token (LocalGuard)."""

import logging
from datetime import datetime

from fastapi import APIRouter, Request
from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict, Field

from app import db

router = APIRouter()
log = logging.getLogger(__name__)
STALE_S = 180
BAD_STATES = {"logged_out", "replaced", "bad_session", "forbidden", "stopped"}
STATE_TEXT = {
    "logged_out": "the WhatsApp session was logged out from the phone; re-pair with --reset-auth",
    "replaced": "another client took over the WhatsApp session",
    "bad_session": "the WhatsApp session data is unreadable; re-pair needed",
    "forbidden": "WhatsApp restricted this account",
    "stopped": "it stopped after too many reconnects",
}


class Heartbeat(BaseModel):
    model_config = ConfigDict(extra="ignore")
    state: str = Field(pattern=r"^(connecting|awaiting_qr|open|reconnecting|logged_out|replaced|bad_session|"
                               r"forbidden|stopped)$")
    detail: str | None = Field(default=None, max_length=200)
    last_message_at: datetime | None = None
    spool_open: int = Field(default=0, ge=0)
    spool_ready: int = Field(default=0, ge=0)
    drop_counts: dict[str, int] = Field(default_factory=dict)
    version: str | None = Field(default=None, max_length=40)


@router.post("/listener/heartbeat")
def heartbeat(request: Request, hb: Heartbeat):
    counts = {k[:40]: v for k, v in list(hb.drop_counts.items())[:50]}
    with db.tx(request.app.state.pool) as conn:
        prev = conn.execute("SELECT state, notified_state FROM listener_status WHERE id = 1").fetchone()
        conn.execute(
            """INSERT INTO listener_status (id, state, detail, last_heartbeat_at, last_message_at, spool_pending,
                                            counts, version)
               VALUES (1, %(state)s, %(detail)s, now(), %(lm)s, %(spool)s, %(counts)s, %(version)s)
               ON CONFLICT (id) DO UPDATE SET state = EXCLUDED.state, detail = EXCLUDED.detail,
                 last_heartbeat_at = now(),
                 last_message_at = coalesce(EXCLUDED.last_message_at, listener_status.last_message_at),
                 spool_pending = EXCLUDED.spool_pending, counts = EXCLUDED.counts, version = EXCLUDED.version""",
            {"state": hb.state, "detail": hb.detail, "lm": hb.last_message_at,
             "spool": hb.spool_open + hb.spool_ready, "counts": Jsonb(counts), "version": hb.version})
        prev_state, notified = prev if prev else (None, None)
        if hb.state != prev_state:
            log.info("listener state %s -> %s", prev_state, hb.state)
        # a deliberate shutdown (SIGTERM, launchd stop) also reports 'stopped', but isn't a failure
        notify = hb.state in BAD_STATES and notified != hb.state and hb.detail != "shutdown"
        if notify or (hb.state == "open" and notified is not None):
            conn.execute("UPDATE listener_status SET notified_state = %s WHERE id = 1",
                         (hb.state if notify else None,))
    if notify:  # one notification per transition into a bad state
        request.app.state.notifier.send("ThriftRadar listener", "Listener stopped: " + STATE_TEXT[hb.state])
    return {"ok": True}


@router.get("/api/listener/status")
def status(request: Request):
    with db.tx(request.app.state.pool) as conn:
        row = conn.execute(
            """SELECT state, detail, last_heartbeat_at, last_message_at, spool_pending, counts, version,
                      extract(epoch FROM now() - last_heartbeat_at)
               FROM listener_status WHERE id = 1""").fetchone()
    if row is None:
        return {"state": "never_seen", "stale": True}
    state, detail, hb_at, msg_at, spool, counts, version, age = row
    return {"state": state, "detail": detail, "last_heartbeat_at": hb_at.isoformat() if hb_at else None,
            "last_message_at": msg_at.isoformat() if msg_at else None, "spool_pending": spool,
            "counts": counts, "version": version, "stale": age is None or float(age) > STALE_S}
