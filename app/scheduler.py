import asyncio
import structlog
from app.config import settings
from app.db import get_pool
from app.repository import (
    fetch_active_gps, save_position, record_failure, mark_completed,
)
from app.services.geocoder import fetch_coords_with_retry

log = structlog.get_logger()

# Порог: после N подряд неудач с валидной ссылкой считаем, что она сгорела
MAX_FAILS_BEFORE_DONE = 5


async def parse_once() -> dict:
    pool = await get_pool()
    rows = await fetch_active_gps(pool)
    log.info("parse_cycle_started", count=len(rows))
    stats = {"total": len(rows), "ok": 0, "failed": 0, "completed": 0}

    for row in rows:
        order_id, url = row["order_id"], row["gps_link"]
        try:
            coords = await fetch_coords_with_retry(url)
            if coords.ok:
                await save_position(pool, order_id, coords.lat, coords.lng)
                stats["ok"] += 1
                log.info("position_saved", order_id=order_id, lat=coords.lat, lng=coords.lng)
            else:
                fails = await record_failure(pool, order_id, "coords_not_found")
                stats["failed"] += 1
                log.warning("coords_not_found", order_id=order_id, fails=fails)
                if fails >= MAX_FAILS_BEFORE_DONE:
                    await mark_completed(pool, order_id)
                    stats["completed"] += 1
                    log.info("marked_completed", order_id=order_id)
        except Exception as e:
            await record_failure(pool, order_id, str(e))
            stats["failed"] += 1
            log.exception("parse_error", order_id=order_id)

    log.info("parse_cycle_finished", **stats)
    return stats


async def scheduler_loop(stop_event: asyncio.Event) -> None:
    while not stop_event.is_set():
        try:
            await parse_once()
        except Exception:
            log.exception("scheduler_crash")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=settings.parser_interval_seconds)
        except asyncio.TimeoutError:
            pass
