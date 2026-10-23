import pytest
from httpx import AsyncClient, ASGITransport

from app.main import app


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.mark.asyncio
async def test_search_too_short(client):
    r = await client.get("/api/shipments?q=1")
    assert r.status_code == 400
    assert "4 цифры" in r.json()["detail"]


@pytest.mark.asyncio
async def test_search_ok(client, db_pool):
    async with db_pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO shipments (order_id, direction, address, status, ship_date)
            VALUES ('ТПК00001806', 'НСК', 'Новосибирск', 'в пути', '2026-10-20')
        """)

    r = await client.get("/api/shipments?q=1806")
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 1
    assert body["items"][0]["order_id"] == "ТПК00001806"


@pytest.mark.asyncio
async def test_get_one_404(client):
    r = await client.get("/api/shipments/НЕТ_ТАКОГО")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_get_one_ok(client, db_pool):
    async with db_pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO shipments (order_id, direction, address, status)
            VALUES ('ТПК00009999', 'МСК', 'Подольск', 'в пути')
        """)

    r = await client.get("/api/shipments/ТПК00009999")
    assert r.status_code == 200
    assert r.json()["order_id"] == "ТПК00009999"


@pytest.mark.asyncio
async def test_list_with_status_filter(client, db_pool):
    async with db_pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO shipments (order_id, direction, status) VALUES
                ('ТПК00000001', 'МСК', 'в пути'),
                ('ТПК00000002', 'НСК', 'завершено')
        """)

    r = await client.get("/api/shipments?status=в пути")
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 1
    assert body["items"][0]["status"] == "в пути"
