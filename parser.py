import asyncio
import asyncpg
import os
import json
import re
import requests
from playwright.async_api import async_playwright

DATABASE_URL = os.environ.get("DATABASE_URL")

# Глобальный sid — переиспользуется между заказами
_current_sid = None


async def get_sid_from_browser(gps_link: str):
    """Открывает GPS-ссылку в Playwright и перехватывает sid из запроса."""
    captured = {"sid": None}

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()

        async def on_request(request):
            url = request.url
            if "wialon/ajax.html" in url and "sid=" in url:
                m = re.search(r"sid=([a-f0-9]+)", url)
                if m and not captured["sid"]:
                    captured["sid"] = m.group(1)
                    print(f"  Перехвачен sid: {captured['sid'][:16]}...")

        page.on("request", on_request)
        try:
            await page.goto(gps_link, timeout=45000, wait_until="domcontentloaded")
            await page.wait_for_timeout(8000)
        except Exception as e:
            print(f"  Playwright ошибка: {e}")
        finally:
            await browser.close()

    return captured["sid"]


def get_units_from_token(token: str):
    """Через token/login получает список ID машин."""
    try:
        r = requests.get(
            "https://maps.baltgps.ru/wialon/ajax.html",
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


def get_position_by_sid(sid: str, unit_id: int):
    """Быстрый запрос координат через requests."""
    try:
        r = requests.get(
            "https://maps.baltgps.ru/wialon/ajax.html",
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


async def ensure_sid(gps_link: str):
    """Гарантирует наличие рабочего sid. Если нет — получает через Playwright."""
    global _current_sid

    if _current_sid:
        # Проверим, работает ли
        token = gps_link.split("?t=")[-1]
        units, _, _ = get_units_from_token(token)
        if units:
            lat, lng, status = get_position_by_sid(_current_sid, units[0])
            if status == "ok":
                return _current_sid
            print(f"  sid протух ({status}), получаю новый через Playwright")

    print(f"  Получаю новый sid через Playwright...")
    _current_sid = await get_sid_from_browser(gps_link)
    return _current_sid


async def process_order(conn, order_id: str, gps_link: str):
    """Обрабатывает один заказ: получает координаты и пишет в БД."""
    token = gps_link.split("?t=")[-1]
    units, eid, host = get_units_from_token(token)

    if not units:
        print(f"  {order_id}: не удалось получить список машин")
        return False

    sid = await ensure_sid(gps_link)
    if not sid:
        print(f"  {order_id}: не удалось получить sid")
        return False

    # Берём первую машину из списка
    unit_id = units[0]
    lat, lng, status = get_position_by_sid(sid, unit_id)

    if status == "sid_expired":
        # Сбросить sid и попробовать ещё раз
        global _current_sid
        _current_sid = None
        sid = await ensure_sid(gps_link)
        lat, lng, status = get_position_by_sid(sid, unit_id) if sid else (None, None, "no_sid")

    if lat and lng:
        await conn.execute("""
            UPDATE shipments
            SET current_lat = $1, current_lng = $2,
                last_position_update = NOW(), updated_at = NOW()
            WHERE order_id = $3
        """, lat, lng, order_id)
        print(f"  {order_id}: {lat}, {lng}")
        return True

    print(f"  {order_id}: координаты не получены ({status})")
    return False


async def main():
    conn = await asyncpg.connect(DATABASE_URL)
    rows = await conn.fetch("""
        SELECT order_id, gps_link FROM shipments
        WHERE status = 'в пути' AND gps_link IS NOT NULL AND gps_link != ''
    """)
    print(f"Найдено {len(rows)} активных заказов")

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
