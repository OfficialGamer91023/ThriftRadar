"""App factory. Spec: DESIGN.md §4.3 `create_app`."""

import json
import logging
import re
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from app import db
from app.api import credits, demo_upload, health, ingest, listener_status, login, posts, search, wishlists
from app.media_store import BundledStore, CompositeStore, LocalDirStore, PgBlobStore
from app.notify import get_notifier
from app.pipeline.models import ModelRegistry
from app.ratelimit import LIMITS, SlidingWindowLimiter
from app.security import BodySizeLimitMiddleware, DemoDenylist, LocalGuard, SecurityHeaders
from app.settings import Settings
from app.worker import LocalCtx, Worker, make_vlm_ctx, sweep_once

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


def demo_media_store(settings: Settings, pool) -> CompositeStore:
    """Seed photos (bundled, read-only; from backend/seed once step 14 adds the manifest), then uploads in the DB."""
    stores = []
    manifest = Path(settings.seed_dir) / "manifest.json"
    if manifest.is_file():
        files = [f for p in json.loads(manifest.read_text())["posts"] for f in p["images"]]
        stores.append(BundledStore(Path(settings.seed_dir) / "images", files))
    stores.append(PgBlobStore(pool))
    return CompositeStore(stores)


def _seed_upload_limit(limiter: SlidingWindowLimiter, pool) -> None:
    """The global daily upload cap counts today's uploads that are already in the DB."""
    with db.tx(pool) as conn:
        ages = [float(r[0]) for r in conn.execute(
            """SELECT extract(epoch FROM now() - created_at) FROM posts
               WHERE source = 'demo_upload' AND created_at > now() - interval '24 hours'""").fetchall()]
    limiter.seed("upload_global", "all", ages)
    log.info("upload_global seeded with %d of %d", len(ages), LIMITS["upload_global"][0])


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
    media_store = demo_media_store(settings, pool) if settings.demo_mode else LocalDirStore(settings.media_dir)
    notifier = get_notifier(settings)  # NullNotifier in demo
    worker = Worker(LocalCtx(pool, settings, models, media_store, notifier),
                    make_vlm_ctx(pool, settings, media_store, notifier))
    limiter = SlidingWindowLimiter()
    if settings.demo_mode:
        _seed_upload_limit(limiter, pool)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if settings.load_models:
            models.load_async()
        try:
            sweep_once(pool, settings, notifier)  # in demo this also purges uploads older than 24 h
        except Exception:
            log.exception("startup sweep failed")
        worker.start()
        yield
        worker.stop()
        pool.close()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.settings = settings
    app.state.pool = pool
    app.state.models = models
    app.state.worker = worker
    app.state.notifier = notifier
    app.state.wake = worker.wake
    app.state.limiter = limiter
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

    mounted = ["health", "search", "wishlists", "posts", "credits"]
    for r in (health.router, search.router, wishlists.router, posts.router, credits.router):
        app.include_router(r)
    if settings.demo_mode:
        app.include_router(login.router)
        app.include_router(demo_upload.router)
        mounted += ["login", "demo_upload"]
    else:
        app.include_router(ingest.router)
        app.include_router(listener_status.router)
        mounted += ["ingest", "listener_status"]
    # Unknown API paths get a JSON 404 for every method, never the web app's HTML or a static-files 405.
    @app.api_route("/api/{rest:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"], include_in_schema=False)
    def api_not_found(rest: str):
        return JSONResponse({"detail": "not_found"}, 404)

    if WEB_OUT.is_dir():
        app.mount("/", StaticFiles(directory=WEB_OUT, html=True), name="web")
        mounted.append("web")

    log.info("app ready mode=%s routers=%s", "demo" if settings.demo_mode else "local", ",".join(mounted))
    return app
