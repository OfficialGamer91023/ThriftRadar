"""Demo login, logout and session check. Spec: DESIGN.md §4.7 `POST /api/login`.
Mounted only when DEMO_MODE=1, and each handler re-checks it: the credentials are public, so they must do
nothing unless the server is in demo mode."""

import hmac
import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.ratelimit import guard, ip
from app.sessions import COOKIE, MAX_AGE_S, new_session_id, session_id, sign

router = APIRouter()
log = logging.getLogger(__name__)


class LoginIn(BaseModel):
    email: str = Field(max_length=200)
    password: str = Field(max_length=200)


def _not_found() -> JSONResponse:
    return JSONResponse({"detail": "not_found"}, 404)


@router.post("/api/login")
def login(request: Request, body: LoginIn):
    s = request.app.state.settings
    if not s.demo_mode:
        return _not_found()
    xff = [p for p in request.headers.get("x-forwarded-for", "").split(",") if p.strip()]
    log.info("login xff_entries=%d", len(xff))  # the count only: verifies TRUST_PROXY_HOPS on the Space
    if limited := guard(request, "login", ip(request)):
        return limited
    email_ok = hmac.compare_digest(body.email.strip().lower().encode(), s.demo_email.lower().encode())
    password_ok = hmac.compare_digest(body.password.encode(), s.demo_password.encode())
    if not (email_ok and password_ok):
        return JSONResponse({"detail": "bad_credentials"}, 401)
    resp = JSONResponse({"ok": True})
    resp.set_cookie(COOKIE, sign(s.session_secret, new_session_id()), max_age=MAX_AGE_S, path="/",
                    httponly=True, secure=True, samesite="lax")
    return resp


@router.post("/api/logout")
def logout(request: Request):
    if not request.app.state.settings.demo_mode:
        return _not_found()
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(COOKIE, path="/", httponly=True, secure=True, samesite="lax")
    return resp


@router.get("/api/session")
def session(request: Request):
    if not request.app.state.settings.demo_mode:
        return _not_found()
    return {"logged_in": session_id(request) is not None}
