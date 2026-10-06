import asyncio
import asyncpg
import os
import json
from playwright.async_api import async_playwright

DATABASE_URL = os.environ.get("DATABASE_URL")


async def get_position(gps_url: str):
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        captured = {}

        async def on_response(response):
            if response.ok and ("locator" in response.url or "avl" in response.url
                                or "get_messages" in response.url):
                try:
                    data = await response.json()
                    text = json.dumps(data)
                    if '"x":' in text and '"y":' in text:
                        captured["data"] = data
                except Exception:
                    pass

        page.on("response", on_response)
        try:
            await page.goto(gps_url, timeout=45000, wait_until="networkidle")
            await page.wait_for_timeout(8000)
        except Exception as e:
            print(f"Ошибка {gps_url}: {e}")
        finally:
            await browser.close()

        return captured.get("data")


def extract_lat_lng(data):
    if not data:
        return None, None

    def find_pos(obj):
        if isinstance(obj, dict):
            if "pos" in obj and isinstance(obj["pos"], dict):
                p = obj["pos"]
                if "x" in p and "y" in p:
                    return p["y"], p["x"]
            for v in obj.values():
                r = find_pos(v)
                if r:
                    return r
        elif isinstance(obj, list):
            for v in obj:
                r = find_pos(v)
                if r:
                    return r
        return None

    return find_pos(data) or (None, None)


async def main():
    conn = await asyncpg.connect(DATABASE_URL)
    rows = await conn.fetch("""
        SELECT order_id, gps_link FROM shipments
        WHERE status = 'в пути' AND gps_link IS NOT NULL AND gps_link != ''
    """)
    print(f"Найдено {len(rows)} активных заказов")

    updated = 0
    for row in rows:
        data = await get_position(row["gps_link"])
        lat, lng = extract_lat_lng(data)
        if lat and lng:
            await conn.execute("""
                UPDATE shipments
                SET current_lat = $1, current_lng = $2,
                    last_position_update = NOW(), updated_at = NOW()
                WHERE order_id = $3
            """, lat, lng, row["order_id"])
            print(f"{row['order_id']}: {lat}, {lng}")
            updated += 1
        else:
            print(f"{row['order_id']}: координаты не найдены")

    await conn.close()
    print(f"Обновлено: {updated}")


if __name__ == "__main__":
    asyncio.run(main())
