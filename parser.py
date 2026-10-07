import asyncio
import asyncpg
import os
import json
import re
import requests
from playwright.async_api import async_playwright

DATABASE_URL = os.environ.get("DATABASE_URL")

_current_sid = None


async def save_position(conn, order_id, lat, lng):
    """Сохраняет позицию. При первом появлении фиксирует ship_lat/ship_lng."""
    row = await conn.fetchrow(
        "SELECT ship_lat FROM shipments WHERE order_id = $1", order_id
    )
    if row and row["ship_lat"] is None:
        # Первый раз видим машину — фиксируем стартовую точку
        await conn.execute("""
            UPDATE shipments
            SET ship_lat = $1, ship_lng = $2,
                current_lat = $1, current_lng = $2,
                first_seen_at = NOW(),
                last_position_update = NOW(),
                updated_at = NOW()
            WHERE order_id = $3
        """, lat, lng, order_id)
    else:
        # Уже видели — обновляем только текущую позицию
        await conn.execute("""
            UPDATE shipments
            SET current_lat = $1, current_lng = $2,
                last_position_update = NOW(),
                updated_at = NOW()
            WHERE order_id = $3
        """, lat, lng, order_id)


async def get_sid_from_maps_baltgps(gps_link: str):
    """maps.baltgps.ru — перехват sid через Playwright."""
    captured = {"sid": None}

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context()
        page = await context.new_page()

        def extract_sid(url):
            if "sid=" not in url:
                return None
            m = re.search(r"[?&]sid=([a-f0-9]{20,})", url)
            return m.group(1) if m else None

        async def on_request(request):
            sid = extract_sid(request.url)
            if sid and not captured["sid"]:
                captured["sid"] = sid
                print(f"  Перехвачен sid: {sid[:16]}...")

        async def on_response(response):
            sid = extract_sid(response.url)
            if sid and not captured["sid"]:
                captured["sid"] = sid
                print(f"  Перехвачен sid из ответа: {sid[:16]}...")

        page.on("request", on_request)
        page.on("response", on_response)

        try:
            await page.goto(gps_link, timeout=60000, wait_until="domcontentloaded")
            await page.wait_for_timeout(12000)
        except Exception as e:
            print(f"  Playwright ошибка: {e}")
        finally:
            await browser.close()

    return captured["sid"]


async def get_coords_from_gs_baltgps(gps_link: str):
    """gs.baltgps.ru — координаты из HTML после клика по карте."""
    coords = {"lat": None, "lng": None}

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()

        try:
            await page.goto(gps_link, timeout=90000, wait_until="domcontentloaded")
            print("  Жду 25 секунд, пока карта прогрузится...")
            await page.wait_for_timeout(25000)

            html = await page.content()
            m = re.findall(r'N(\d+\.\d+)[,\s]*E(\d+\.\d+)', html)

            if m:
                coords["lat"] = float(m[-1][0])
                coords["lng"] = float(m[-1][1])
                print(f"  Найдено из HTML: {coords['lat']}, {coords['lng']}")
            else:
                print("  Координаты в HTML не найдены, кликаю по карте...")
                await page.mouse.click(900, 400)
                await page.wait_for_timeout(3000)

                html = await page.content()
                m = re.findall(r'N(\d+\.\d+)[,\s]*E(\d+\.\d+)', html)
                if m:
                    coords["lat"] = float(m[-1][0])
                    coords["lng"] = float(m[-1][1])
                    print(f"  Найдено после клика: {coords['lat']}, {coords['lng']}")
                else:
                    print(f"  Координаты не найдены")
        except Exception as e:
            print(f"  Ошибка Playwright: {e}")
        finally:
            await browser.close()

    return coords["lat"], coords["lng"]


def get_units_from_token(token: str, base_url: str = "https://maps.baltgps.ru"):
    """token/login — для maps.baltgps.ru."""
    try:
        r = requests.get(
            f"{base_url}/wialon/ajax.html",
            params={
                "svc": "token/login",
                "params": json.dumps({"token": token})
            },
            timeout=15
        )
        data = r.json()
        if "token" in data:
            token_data = json.loads(data["token"])
            return token_data.get("items", []), data.get("eid"), data.get("host")
    except Exception as e:
        print(f"  token/login ошибка: {e}")
    return [], None, None


def get_position_by_sid(sid: str, unit_id: int, base_url: str = "https://maps.baltgps.ru"):
    """core/search_item — для maps.baltgps.ru."""
    try:
        r = requests.get(
            f"{base_url}/wialon/ajax.html",
            params={
                "svc": "core/search_item",
                "params": json.dumps({"id": unit_id, "flags": 1025}),
                "sid": sid
            },
            timeout=10
        )
        if r.status_code != 200:
            return None, None, "http_error"
        data = r.json()
        if "error" in data:
            return None, None, "sid_expired"
        item = data.get("item", {})
        pos = item.get("pos")
        if pos and "x" in pos and "y" in pos:
            return pos["y"], pos["x"], "ok"
    except Exception as e:
        print(f"  search_item ошибка: {e}")
    return None, None, "unknown"


async def process_order(conn, order_id: str, gps_link: str):
    """Определяет тип ссылки и обрабатывает соответственно."""
    global _current_sid

    # === Тип 1: gs.baltgps.ru (новый интерфейс) ===
    if "gs.baltgps.ru" in gps_link:
        print(f"  {order_id}: тип gs.baltgps.ru (через HTML)")
        lat, lng = await get_coords_from_gs_baltgps(gps_link)
        if lat and lng:
            await save_position(conn, order_id, lat, lng)
            print(f"  {order_id}: {lat}, {lng} ✅")
            return True
        print(f"  {order_id}: координаты не получены ❌")
        return False

    # === Тип 2: maps.baltgps.ru (старый интерфейс) ===
    if "maps.baltgps.ru" in gps_link:
        print(f"  {order_id}: тип maps.baltgps.ru (через API)")
        token = gps_link.split("?t=")[-1]
        units, eid, host = get_units_from_token(token)

        if not units:
            print(f"  {order_id}: не удалось получить список машин")
            return False

        print(f"  Получаю новый sid через Playwright...")
        _current_sid = await get_sid_from_maps_baltgps(gps_link)

        if not _current_sid:
            print(f"  {order_id}: не удалось получить sid")
            return False

        unit_id = units[0]
        lat, lng, status = get_position_by_sid(_current_sid, unit_id)

        if lat and lng:
            await save_position(conn, order_id, lat, lng)
            print(f"  {order_id}: {lat}, {lng} ✅")
            return True

        print(f"  {order_id}: координаты не получены ({status})")
        return False

    # === Тип 3: неизвестный сервис ===
    print(f"  {order_id}: неизвестный GPS-сервис ({gps_link[:60]}), пропускаю")
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
            ok = await process_order(conn, row["order_id"], row["gps_link"])
            if ok:
                updated += 1
        except Exception as e:
            print(f"  {row['order_id']}: ошибка {e}")

    await conn.close()
    print(f"\nОбновлено: {updated} из {len(rows)}")


if __name__ == "__main__":
    asyncio.run(main())
