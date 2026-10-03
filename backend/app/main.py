"""App factory. Spec: DESIGN.md §4.3 `create_app`."""

import logging
import re
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app import db
from app.api import health, ingest
from app.media_store import LocalDirStore
from app.pipeline.models import ModelRegistry
from app.security import BodySizeLimitMiddleware, DemoDenylist, LocalGuard, SecurityHeaders
from app.settings import Settings
from app.worker import LocalCtx, Worker, sweep_once

log = logging.getLogger(__name__)

WEB_OUT = Path(__file__).resolve().parents[2] / "web" / "out"
MB = 1024 * 1024
KB = 1024


class StartupError(Exception):
    pass


class RedactFilter(logging.Filter):
    """Last line of defence: mask any run of 7+ digits (phone numbers) in log output."""

    DIGITS = re.compile(r"\d{7,}")

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        masked = self.DIGITS.sub(lambda m: m.group()[:2] + "*" * (len(m.group()) - 2), msg)
        if masked != msg:
            record.msg, record.args = masked, None
        return True


def configure_logging() -> None:
    root = logging.getLogger()
    if not any(isinstance(f, RedactFilter) for h in root.handlers for f in h.filters):
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
        handler.addFilter(RedactFilter())
        root.addHandler(handler)
    root.setLevel(logging.INFO)


def _startup_checks(settings: Settings, pool) -> None:
    settings.check_startup()
    with db.tx(pool) as conn:
        row = conn.execute("SELECT value FROM db_meta WHERE key = 'role'").fetchone()
        role = row[0] if row else None
        expected = "demo" if settings.demo_mode else "local"
        if role != expected:
            raise StartupError(f"db_meta.role is {role!r} but this process expects {expected!r}")
        if settings.demo_mode:
            real = conn.execute(
                "SELECT count(*) FROM posts WHERE source IN ('whatsapp', 'chat_export')").fetchone()[0]
            if real:
                raise StartupError("demo DB contains whatsapp/chat_export posts")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    configure_logging()
    pool = db.make_pool(settings.database_url, settings.db_pool_max)
    try:
        _startup_checks(settings, pool)
    except Exception:
        pool.close()
        raise

    models = ModelRegistry(settings.models_dir, settings.demo_mode)
    media_store = None if settings.demo_mode else LocalDirStore(settings.media_dir)
    # The demo media store (bundled seed + Postgres blobs) arrives in build step 13; no worker until then.
    worker = Worker(LocalCtx(pool, settings, models, media_store)) if media_store is not None else None

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if settings.load_models:
            models.load_async()
        if worker is not None:
            try:
                sweep_once(pool, settings)
            except Exception:
                log.exception("startup sweep failed")
            worker.start()
        # Step 13 adds purge_demo_uploads().
        yield
        if worker is not None:
            worker.stop()
        pool.close()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.settings = settings
    app.state.pool = pool
    app.state.models = models
    app.state.worker = worker
    app.state.wake = worker.wake if worker is not None else (lambda: None)
    app.state.media_store = media_store

    # Middleware: the last one added runs first. Order of checks: headers wrapper → guard → body limit.
    app.add_middleware(BodySizeLimitMiddleware, limits={
        "/ingest": settings.ingest_max_bytes,
        "/api/demo/posts": settings.upload_max_images * settings.upload_max_bytes + 64 * KB,
        "/api/search/image": settings.upload_max_bytes + 64 * KB,
        "/api/wishlists": settings.upload_max_bytes + 64 * KB,
    })
    if settings.demo_mode:
        app.add_middleware(DemoDenylist)
    else:
        app.add_middleware(LocalGuard, ingest_token=settings.ingest_token)
    app.add_middleware(SecurityHeaders)

    mounted = ["health"]
    app.include_router(health.router)
    if not settings.demo_mode:
        app.include_router(ingest.router)
        mounted.append("ingest")
    if WEB_OUT.is_dir():
        app.mount("/", StaticFiles(directory=WEB_OUT, html=True), name="web")
        mounted.append("web")

    log.info("app ready mode=%s routers=%s", "demo" if settings.demo_mode else "local", ",".join(mounted))
    return app
