import asyncio
import time
from app.config import settings


class SidCache:
    """TTL-кэш SID для maps.baltgps.ru. Один SID на токен."""

    def __init__(self):
        self._data: dict[str, tuple[str, float]] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def get(self, token: str) -> str | None:
        item = self._data.get(token)
        if item is None:
            return None
        sid, expires_at = item
        if time.monotonic() > expires_at:
            self._data.pop(token, None)
            return None
        return sid

    def put(self, token: str, sid: str) -> None:
        self._data[token] = (sid, time.monotonic() + settings.sid_cache_ttl_seconds)

    def invalidate(self, token: str) -> None:
        self._data.pop(token, None)

    def lock(self, token: str) -> asyncio.Lock:
        return self._locks.setdefault(token, asyncio.Lock())


sid_cache = SidCache()
