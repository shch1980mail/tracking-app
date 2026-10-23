import asyncio
import structlog
from app.config import settings
from app.services.providers.base import Coords
from app.services.providers.axenta import AxentaProvider
from app.services.providers.gs_baltgps import GsBaltgpsProvider
from app.services.providers.maps_baltgps import MapsBaltgpsProvider

log = structlog.get_logger()

PROVIDERS = [AxentaProvider(), GsBaltgpsProvider(), MapsBaltgpsProvider()]


def resolve_provider(url: str):
    for p in PROVIDERS:
        if p.matches(url):
            return p
    return None


async def fetch_coords_with_retry(url: str) -> Coords:
    provider = resolve_provider(url)
    if provider is None:
        log.warning("unknown_provider", url=url)
        return Coords(None, None)

    for attempt in range(1, settings.max_retries + 1):
        coords = await provider.fetch(url)
        if coords.ok:
            return coords
        if attempt < settings.max_retries:
            delay = settings.retry_backoff_seconds * (2 ** (attempt - 1))
            log.info("retry", provider=provider.name, attempt=attempt, delay=delay)
            await asyncio.sleep(delay)

    return Coords(None, None)
