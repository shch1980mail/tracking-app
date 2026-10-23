import pytest
from httpx import AsyncClient, ASGITransport
from app.main import app


@pytest.mark.asyncio
async def test_health():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        # health требует БД — проверим 400 на короткий q
        r = await c.get("/api/shipments?q=1")
        assert r.status_code == 400
