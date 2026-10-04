"""Demo sessions: a signed cookie holding a random session id. Spec: DESIGN.md §4.7 (login; step 13 decisions).
Local mode has no sessions: the owner is always 'local' and everything is visible."""

import base64
import hashlib
import hmac
import time
import uuid

from fastapi import Request
from fastapi.responses import JSONResponse

COOKIE = "tr_session"
MAX_AGE_S = 86400
LOCAL_OWNER = "local"


def _mac(secret: str, payload: str) -> str:
    d = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(d).rstrip(b"=").decode()


def sign(secret: str, session_id: str, now: float | None = None) -> str:
    payload = f"{session_id}.{int(now if now is not None else time.time())}"
    return f"{payload}.{_mac(secret, payload)}"


def unsign(secret: str, token: str, now: float | None = None) -> str | None:
    """-> session id, or None if the token is malformed, forged or older than 24 h."""
    parts = token.split(".")
    if len(parts) != 3:
        return None
    sid, ts, mac = parts
    if not hmac.compare_digest(mac.encode(), _mac(secret, f"{sid}.{ts}").encode()):
        return None
    try:
        uuid.UUID(sid)
        age = (now if now is not None else time.time()) - int(ts)
    except ValueError:
        return None
    return sid if 0 <= age <= MAX_AGE_S else None


def new_session_id() -> str:
    return str(uuid.uuid4())


def session_id(request: Request) -> str | None:
    s = request.app.state.settings
    token = request.cookies.get(COOKIE)
    if not s.demo_mode or not token or not s.session_secret:
        return None
    return unsign(s.session_secret, token)


class Viewer:
    """Who is asking. `owner` keys wishlists; `visible_to` is None (everything) locally, the session id in demo."""

    def __init__(self, owner: str, visible_to: str | None):
        self.owner = owner
        self.visible_to = visible_to


def viewer(request: Request) -> Viewer | JSONResponse:
    """Local: everything. Demo: the session from the cookie, or a 401 response to return as is."""
    if not request.app.state.settings.demo_mode:
        return Viewer(LOCAL_OWNER, None)
    sid = session_id(request)
    if sid is None:
        return JSONResponse({"detail": "login_required"}, 401)
    return Viewer(sid, sid)
