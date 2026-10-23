"""Загружает shipments (2).json в PostgreSQL (upsert по order_id)."""
import asyncio
import json
import sys
from datetime import datetime, date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from app.db import get_pool, close_pool


def _parse_date(v):
    if not v:
        return None
    return datetime.strptime(v, "%Y-%m-%d").date()


async def main(json_path: str):
    data = json.loads(Path(json_path).read_text(encoding="utf-8"))
    pool = await get_pool()
    async with pool.acquire() as conn:
        for order_id, s in data.items():
            await conn.execute("""
                INSERT INTO shipments (
                    order_id, direction, address, weight, ship_date, plan_arrival,
                    carrier, vehicle, driver, gps_link, comment, status
                ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12)
                ON CONFLICT (order_id) DO UPDATE SET
                    direction    = EXCLUDED.direction,
                    address      = EXCLUDED.address,
                    weight       = EXCLUDED.weight,
                    ship_date    = EXCLUDED.ship_date,
                    plan_arrival = EXCLUDED.plan_arrival,
                    carrier      = EXCLUDED.carrier,
                    vehicle      = EXCLUDED.vehicle,
                    driver       = EXCLUDED.driver,
                    gps_link     = EXCLUDED.gps_link,
                    comment      = EXCLUDED.comment,
                    status       = EXCLUDED.status,
                    updated_at   = NOW()
            """,
                s.get("order_id"), s.get("direction"), s.get("address"),
                s.get("weight"), _parse_date(s.get("ship_date")),
                _parse_date(s.get("plan_arrival")), s.get("carrier"),
                s.get("vehicle"), s.get("driver"), s.get("gps_link"),
                s.get("comment"), s.get("status") or "планируется",
            )
    await close_pool()
    print(f"Импортировано/обновлено: {len(data)}")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else "shipments (2).json"))
