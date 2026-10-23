import time
import pytest

from app.services.sid_cache import SidCache


def test_put_get():
    c = SidCache()
    c.put("tok", "sid123")
    assert c.get("tok") == "sid123"


def test_invalidate():
    c = SidCache()
    c.put("tok", "sid123")
    c.invalidate("tok")
    assert c.get("tok") is None


def test_ttl_expiry(monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "sid_cache_ttl_seconds", 0)

    c = SidCache()
    c.put("tok", "sid123")
    time.sleep(0.01)
    assert c.get("tok") is None


@pytest.mark.asyncio
async def test_lock_is_stable():
    c = SidCache()
    l1 = c.lock("tok")
    l2 = c.lock("tok")
    assert l1 is l2
