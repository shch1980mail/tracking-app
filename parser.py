import asyncio
import asyncpg
import os
import json
import re
import requests
from playwright.async_api import async_playwright

DATABASE_URL = os.environ.get("DATABASE_URL")
_current_sid = None

async def mark_as_done(conn, order_id):
    """Помечает заказ как завершённый, если ссылка сгорела."""
    print(f"  {order_id}: ссылка невалидна, помечаю как 'завершено'")
    await conn.execute("""
        UPDATE shipments
        SET status = 'завершено', current_lat = NULL, current_lng = NULL
        WHERE order_id = $1
    """, order_id)

async def save_position(conn, order_id, lat, lng):
    """Сохраняет позицию. При первом появлении фиксирует ship_lat/ship_lng."""
    row = await conn.fetchrow("SELECT ship_lat FROM shipments WHERE order_id = $1", order_id)
    if row and row["ship_lat"] is None:
        await conn.execute("""
            UPDATE shipments
            SET ship_lat = $1, ship_lng = $2, current_lat = $1, current_lng = $2,
                first_seen_at = NOW(), last_position_update = NOW(), updated_at = NOW()
            WHERE order_id = $3
        """, lat, lng, order_id)
    else:
        await conn.execute("""
            UPDATE shipments
            SET current_lat = $1, current_lng = $2, last_position_update = NOW(), updated_at = NOW()
            WHERE order_id = $3
        """, lat, lng, order_id)

async def get_sid_from_maps_baltgps(gps_link: str):
    captured = {"sid": None}
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context()
        page = await context.new_page()

        def extract_sid(url):
            m = re.search(r"[?&]sid=([a-f0-9]{20,})", url)
            return m.group(1) if m else None

        page.on("request", lambda req: captured.update({"sid": extract_sid(req.url)}) if extract_sid(req.url) and not captured["sid"] else None)
        page.on("response", lambda res: captured.update({"sid": extract_sid(res.url)}) if extract_sid(res.url) and not captured["sid"] else None)

        try:
            await page.goto(gps_link, timeout=60000, wait_until="domcontentloaded")
            await page.wait_for_timeout(12000)
        except Exception as e:
            print(f"  Playwright ошибка: {e}")
        finally:
            await browser.close()
    return captured["sid"]

async def get_coords_from_gs_baltgps(gps_link: str):
    coords = {"lat": None, "lng": None}
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        try:
            await page.goto(gps_link, timeout=90000, wait_until="domcontentloaded")
            await page.wait_for_timeout(25000)
            html = await page.content()
            m = re.findall(r'N(\d+\.\d+)[,\s]*E(\d+\.\d+)', html)
            if m:
                coords["lat"], coords["lng"] = float(m[-1][0]), float(m[-1][1])
            else:
                await page.mouse.click(900, 400)
                await page.wait_for_timeout(3000)
                html = await page.content()
                m = re.findall(r'N(\d+\.\d+)[,\s]*E(\d+\.\d+)', html)
                if m:
                    coords["lat"], coords["lng"] = float(m[-1][0]), float(m[-1][1])
        except Exception as e:
            print(f"  Ошибка Playwright: {e}")
        finally:
            await browser.close()
    return coords["lat"], coords["lng"]

def get_units_from_token(token: str, base_url: str = "https://maps.baltgps.ru"):
    try:
        r = requests.get(f"{base_url}/wialon/ajax.html", params={"svc": "token/login", "params": json.dumps({"token": token})}, timeout=15)
        data = r.json()
        if "token" in data:
            return json.loads(data["token"]).get("items", []), data.get("eid"), data.get("host")
    except Exception as e:
        print(f"  token/login ошибка: {e}")
    return [], None, None

def get_position_by_sid(sid: str, unit_id: int, base_url: str = "https://maps.baltgps.ru"):
    try:
        r = requests.get(f"{base_url}/wialon/ajax.html", params={"svc": "core/search_item", "params": json.dumps({"id": unit_id, "flags": 1025}), "sid": sid}, timeout=10)
        data = r.json()
        if "error" in data: return None, None, "sid_expired"
        item = data.get("item", {})
        pos = item.get("pos")
        if pos and "x" in pos and "y" in pos: return pos["y"], pos["x"], "ok"
    except Exception as e:
        print(f"  search_item ошибка: {e}")
    return None, None, "unknown"

async def process_order(conn, order_id: str, gps_link: str):
    global _current_sid

    if "gs.baltgps.ru" in gps_link:
        lat, lng = await get_coords_from_gs_baltgps(gps_link)
        if lat and lng:
            await save_position(conn, order_id, lat, lng)
            return True
        await mark_as_done(conn, order_id)
        return False

    if "maps.baltgps.ru" in gps_link:
        token = gps_link.split("?t=")[-1]
        units, _, _ = get_units_from_token(token)
        if not units:
            await mark_as_done(conn, order_id)  # Ссылка сгорела
            return False

        _current_sid = await get_sid_from_maps_baltgps(gps_link)
        if not _current_sid:
            await mark_as_done(conn, order_id)
            return False

        lat, lng, status = get_position_by_sid(_current_sid, units[0])
        if lat and lng:
            await save_position(conn, order_id, lat, lng)
            return True
        
        if status == "sid_expired":
            await mark_as_done(conn, order_id)
        return False

    return False

async def main():
    conn = await asyncpg.connect(DATABASE_URL)
    rows = await conn.fetch("""
        SELECT order_id, gps_link FROM shipments
        WHERE status = 'в пути' AND gps_link IS NOT NULL AND gps_link != ''
    """)
    print(f"Найдено {len(rows)} активных заказов\n")
    updated = 0
    for row in rows:
        try:
            if await process_order(conn, row["order_id"], row["gps_link"]):
                updated += 1
        except Exception as e:
            print(f"  {row['order_id']}: ошибка {e}")
    await conn.close()
    print(f"\nОбновлено: {updated} из {len(rows)}")

if __name__ == "__main__":
    asyncio.run(main())
