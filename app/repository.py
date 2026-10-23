import asyncpg


async def fetch_active_gps(pool: asyncpg.Pool) -> list[asyncpg.Record]:
    async with pool.acquire() as conn:
        return await conn.fetch("""
            SELECT order_id, gps_link
            FROM shipments
            WHERE status IN ('в пути', 'планируется')
              AND gps_link IS NOT NULL AND gps_link <> ''
              AND gps_link LIKE 'http%'
        """)


async def save_position(pool: asyncpg.Pool, order_id: str, lat: float, lng: float) -> None:
    async with pool.acquire() as conn:
        await conn.execute("""
            UPDATE shipments
            SET ship_lat     = COALESCE(ship_lat, $1),
                ship_lng     = COALESCE(ship_lng, $2),
                current_lat  = $1,
                current_lng  = $2,
                first_seen_at = COALESCE(first_seen_at, NOW()),
                last_position_update = NOW(),
                last_parse_attempt   = NOW(),
                last_parse_error     = NULL,
                parse_fail_count     = 0,
                updated_at           = NOW()
            WHERE order_id = $3
        """, lat, lng, order_id)


async def record_failure(pool: asyncpg.Pool, order_id: str, error: str) -> int:
    async with pool.acquire() as conn:
        row = await conn.fetchrow("""
            UPDATE shipments
            SET last_parse_attempt = NOW(),
                last_parse_error   = $2,
                parse_fail_count   = parse_fail_count + 1,
                updated_at         = NOW()
            WHERE order_id = $1
            RETURNING parse_fail_count
        """, order_id, error[:500])
        return row["parse_fail_count"] if row else 0


async def mark_completed(pool: asyncpg.Pool, order_id: str) -> None:
    async with pool.acquire() as conn:
        await conn.execute("""
            UPDATE shipments
            SET status = 'завершено',
                current_lat = NULL, current_lng = NULL,
                updated_at = NOW()
            WHERE order_id = $1
        """, order_id)


async def search_shipments(pool: asyncpg.Pool, digits: str, limit: int, offset: int):
    async with pool.acquire() as conn:
        total = await conn.fetchval("""
            SELECT COUNT(*) FROM shipments WHERE order_id ILIKE $1
        """, f"%{digits}%")
        rows = await conn.fetch("""
            SELECT order_id, direction, address, weight, ship_date, plan_arrival,
                   carrier, vehicle, status, current_lat, current_lng,
                   last_position_update, comment
            FROM shipments
            WHERE order_id ILIKE $1
            ORDER BY ship_date DESC NULLS LAST
            LIMIT $2 OFFSET $3
        """, f"%{digits}%", limit, offset)
        return total, rows


async def get_shipment(pool: asyncpg.Pool, order_id: str):
    async with pool.acquire() as conn:
        return await conn.fetchrow("""
            SELECT * FROM shipments WHERE order_id = $1
        """, order_id)


async def list_shipments(pool: asyncpg.Pool, status: str | None, limit: int, offset: int):
    async with pool.acquire() as conn:
        where = "WHERE status = $1" if status else ""
        args = [status] if status else []
        total = await conn.fetchval(f"SELECT COUNT(*) FROM shipments {where}", *args)
        rows = await conn.fetch(f"""
            SELECT order_id, direction, address, weight, ship_date, plan_arrival,
                   carrier, vehicle, status, current_lat, current_lng,
                   last_position_update, comment
            FROM shipments {where}
            ORDER BY ship_date DESC NULLS LAST
            LIMIT ${len(args)+1} OFFSET ${len(args)+2}
        """, *args, limit, offset)
        return total, rows
