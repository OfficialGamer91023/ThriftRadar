"""GET /healthz and GET /api/config. Spec: DESIGN.md §4.7. No DB, so health checks never wake Neon."""

from fastapi import APIRouter, Request

router = APIRouter()


@router.get("/healthz")
def healthz(request: Request):
    state = request.app.state
    return {
        "ok": True,
        "models_ready": state.models.ready.is_set(),
        "models_failed": state.models.failed.is_set(),
        "demo": state.settings.demo_mode,
    }


@router.get("/api/config")
def config(request: Request):
    demo = request.app.state.settings.demo_mode
    return {"demo": demo, "features": {"whatsapp": not demo}}
