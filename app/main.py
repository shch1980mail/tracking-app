import asyncio
import contextlib
from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pathlib import Path
import structlog

from app.config import settings
from app.db import get_pool, close_pool
from app.api import shipments, health
from app.scheduler import scheduler_loop

structlog.configure(
    processors=[
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.add_log_level,
        structlog.processors.JSONRenderer(),
    ],
    wrapper_class=structlog.make_filtering_bound_logger(
        getattr(__import__("logging"), settings.log_level)
    ),
)
log = structlog.get_logger()

STATIC_DIR = Path(__file__).parent.parent / "static"


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    await get_pool()
    stop_event = asyncio.Event()
    task = asyncio.create_task(scheduler_loop(stop_event))
    log.info("app_started", interval=settings.parser_interval_seconds)
    yield
    stop_event.set()
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    await close_pool()
    log.info("app_stopped")


def create_app() -> FastAPI:
    app = FastAPI(title="Tracking App", version="2.0.0", lifespan=lifespan)
    app.include_router(health.router)
    app.include_router(shipments.router)

    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/", include_in_schema=False)
    async def index():
        return FileResponse(STATIC_DIR / "index.html")

    return app


app = create_app()
