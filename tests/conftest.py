import asyncio
import os
import pytest
import asyncpg

# До импорта app.* — чтобы config подхватил тестовый URL
os.environ.setdefault(
    "DATABASE_URL",
    os.environ.get("TEST_DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/tracking_test"),
)


@pytest.fixture(scope="session")
def event_loop():
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest.fixture
async def db_pool():
    """Пул к тестовой БД. Пропускается, если БД недоступна."""
    try:
        pool = await asyncpg.create_pool(os.environ["DATABASE_URL"], min_size=1, max_size=2)
    except Exception as e:
        pytest.skip(f"PostgreSQL недоступен: {e}")

    # Минимальная схема для тестов API
    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS shipments (
                order_id TEXT PRIMARY KEY,
                direction TEXT, address TEXT, weight NUMERIC(10,2),
                ship_date DATE, plan_arrival DATE,
                carrier TEXT, vehicle TEXT, driver TEXT,
                gps_link TEXT, comment TEXT,
                status TEXT NOT NULL DEFAULT 'планируется',
                ship_lat DOUBLE PRECISION, ship_lng DOUBLE PRECISION,
                current_lat DOUBLE PRECISION, current_lng DOUBLE PRECISION,
                first_seen_at TIMESTAMPTZ, last_position_update TIMESTAMPTZ,
                last_parse_attempt TIMESTAMPTZ, last_parse_error TEXT,
                parse_fail_count INTEGER NOT NULL DEFAULT 0,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)
        await conn.execute("DELETE FROM shipments")

    yield pool
    await pool.close()
