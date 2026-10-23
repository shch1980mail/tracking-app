import asyncio
import asyncpg
import os
import json
import re
import requests
from playwright.async_api import async_playwright
from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse, JSONResponse

DATABASE_URL = os.environ.get("DATABASE_URL")
_current_sid = None

app = FastAPI(title="Tracking App")


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
            print(f"  Ошибка Playwright (gs): {e}")
        finally:
            await browser.close()

    return coords["lat"], coords["lng"]


async def get_coords_from_axenta(gps_link: str):
    """axenta.cloud — координаты из JSON или HTML."""
    coords = {"lat": None, "lng": None}

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()

        try:
            await page.goto(gps_link, timeout=90000, wait_until="domcontentloaded")
            print("  Axenta: жду 25 секунд...")
            await page.wait_for_timeout(25000)

            html = await page.content()
            m = re.findall(r'"lat"\s*:\s*(\d+\.\d+).*?"lng"\s*:\s*(\d+\.\d+)', html)

            if m:
                coords["lat"], coords["lng"] = float(m[0][0]), float(m[0][1])
                print(f"  Axenta (из JSON): {coords['lat']}, {coords['lng']}")
            else:
                await page.mouse.click(900, 400)
                await page.wait_for_timeout(3000)

                html = await page.content()
                m = re.findall(r'N(\d+\.\d+)[,\s]*E(\d+\.\d+)', html)
                if m:
                    coords["lat"], coords["lng"] = float(m[-1][0]), float(m[-1][1])
                    print(f"  Axenta (из HTML): {coords['lat']}, {coords['lng']}")
                else:
                    print("  Axenta: координаты не найдены")
        except Exception as e:
            print(f"  Ошибка Playwright (axenta): {e}")
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
            return json.loads(data["token"]).get("items", []), data.get("eid"), data.get("host")
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

    if not gps_link or not gps_link.startswith("http"):
        return False

    # === Тип 1: axenta.cloud ===
    if "axenta.cloud" in gps_link:
        print(f"  {order_id}: тип axenta.cloud")
        lat, lng = await get_coords_from_axenta(gps_link)
        if lat and lng:
            await save_position(conn, order_id, lat, lng)
            print(f"  {order_id}: {lat}, {lng} ✅")
            return True
        await mark_as_done(conn, order_id)
        return False

    # === Тип 2: gs.baltgps.ru ===
    if "gs.baltgps.ru" in gps_link:
        print(f"  {order_id}: тип gs.baltgps.ru")
        lat, lng = await get_coords_from_gs_baltgps(gps_link)
        if lat and lng:
            await save_position(conn, order_id, lat, lng)
            print(f"  {order_id}: {lat}, {lng} ✅")
            return True
        await mark_as_done(conn, order_id)
        return False

    # === Тип 3: maps.baltgps.ru ===
    if "maps.baltgps.ru" in gps_link:
        print(f"  {order_id}: тип maps.baltgps.ru")
        token = gps_link.split("?t=")[-1]
        units, eid, host = get_units_from_token(token)

        if not units:
            print(f"  {order_id}: не удалось получить список машин")
            await mark_as_done(conn, order_id)
            return False

        print(f"  Получаю новый sid через Playwright...")
        _current_sid = await get_sid_from_maps_baltgps(gps_link)

        if not _current_sid:
            print(f"  {order_id}: не удалось получить sid")
            await mark_as_done(conn, order_id)
            return False

        unit_id = units[0]
        lat, lng, status = get_position_by_sid(_current_sid, unit_id)

        if lat and lng:
            await save_position(conn, order_id, lat, lng)
            print(f"  {order_id}: {lat}, {lng} ✅")
            return True

        print(f"  {order_id}: координаты не получены ({status})")
        if status == "sid_expired":
            await mark_as_done(conn, order_id)
        return False

    return False


async def main():
    conn = await asyncpg.connect(DATABASE_URL)
    rows = await conn.fetch("""
        SELECT order_id, gps_link FROM shipments
        WHERE status IN ('в пути', 'планируется') 
          AND gps_link IS NOT NULL 
          AND gps_link != ''
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


# ============================================================
# ДОБАВЛЕНО: ВЕБ-ИНТЕРФЕЙС И API ПОИСКА
# ============================================================

HTML_PAGE = """
<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Отслеживание отправлений</title>
    <style>
        * { box-sizing: border-box; }
        body {
            font-family: -apple-system, Segoe UI, Roboto, Arial, sans-serif;
            background: #f5f6f8;
            margin: 0;
            padding: 32px 16px;
            color: #1f2933;
        }
        .container { max-width: 900px; margin: 0 auto; }
        h1 { font-size: 20px; margin: 0 0 8px; }
        .hint { font-size: 13px; color: #6b7280; margin: 0 0 16px; }

        .search-wrap { position: relative; margin-bottom: 16px; }
        .search-wrap .icon {
            position: absolute;
            left: 14px;
            top: 50%;
            transform: translateY(-50%);
            width: 18px;
            height: 18px;
            color: #6b7280;
            pointer-events: none;
        }
        .search-wrap input {
            width: 100%;
            padding: 12px 14px 12px 42px;
            font-size: 15px;
            border: 1px solid #d1d5db;
            border-radius: 10px;
            background: #fff;
            outline: none;
            transition: border-color .15s, box-shadow .15s;
        }
        .search-wrap input:focus {
            border-color: #2563eb;
            box-shadow: 0 0 0 3px rgba(37, 99, 235, .15);
        }

        table {
            width: 100%;
            border-collapse: collapse;
            background: #fff;
            border-radius: 10px;
            overflow: hidden;
            box-shadow: 0 1px 3px rgba(0,0,0,.06);
        }
        th, td {
            text-align: left;
            padding: 12px 14px;
            font-size: 14px;
            border-bottom: 1px solid #eef0f3;
        }
        th {
            background: #f9fafb;
            font-weight: 600;
            color: #4b5563;
        }
        tr:last-child td { border-bottom: none; }

        .status {
            display: inline-block;
            padding: 3px 8px;
            border-radius: 999px;
            font-size: 12px;
            font-weight: 500;
        }
        .status.in-transit { background: #e0f2fe; color: #0369a1; }
        .status.delivered  { background: #dcfce7; color: #15803d; }
        .status.pending    { background: #fef3c7; color: #b45309; }

        .empty {
            padding: 24px;
            text-align: center;
            color: #6b7280;
            background: #fff;
            border-radius: 10px;
            display: none;
        }
    </style>
</head>
<body>
<div class="container">
    <h1>Отслеживание отправлений</h1>
    <p class="hint">Введите минимум 4 цифры из номера отслеживания (например, 1806).</p>

    <div class="search-wrap">
        <svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
            <circle cx="11" cy="11" r="7"></circle>
            <line x1="20" y1="20" x2="16.65" y2="16.65"></line>
        </svg>
        <input id="search" type="text" placeholder="Поиск по последним 4 цифрам..." autocomplete="off">
    </div>

    <table id="table">
        <thead>
            <tr>
                <th>Номер</th>
                <th>Направление</th>
                <th>Адрес</th>
                <th>Статус</th>
            </tr>
        </thead>
        <tbody id="tbody"></tbody>
    </table>
    <div id="empty" class="empty">Ничего не найдено</div>
</div>

<script>
    const input = document.getElementById('search');
    const tbody = document.getElementById('tbody');
    const empty = document.getElementById('empty');
    const table = document.getElementById('table');

    let timer = null;

    function statusClass(status) {
        if (status === 'в пути' || status === 'планируется') return 'in-transit';
        if (status === 'завершено') return 'delivered';
        return 'pending';
    }

    async function load(q) {
        const digits = (q.match(/\\d/g) || []).length;
        if (digits < 4) {
            tbody.innerHTML = '';
            table.style.display = 'none';
            empty.style.display = 'block';
            empty.textContent = 'Введите минимум 4 цифры для поиска';
            return;
        }

        const res = await fetch('/api/shipments?q=' + encodeURIComponent(q));
        const data = await res.json();

        if (!data.length) {
            tbody.innerHTML = '';
            table.style.display = 'none';
            empty.style.display = 'block';
            empty.textContent = 'Ничего не найдено';
            return;
        }

        table.style.display = 'table';
        empty.style.display = 'none';
        tbody.innerHTML = data.map(item => `
            <tr>
                <td>${item.order_id ?? ''}</td>
                <td>${item.direction ?? ''}</td>
                <td>${item.address ?? ''}</td>
                <td><span class="status ${statusClass(item.status)}">${item.status ?? ''}</span></td>
            </tr>
        `).join('');
    }

    input.addEventListener('input', () => {
        clearTimeout(timer);
        timer = setTimeout(() => load(input.value.trim()), 200);
    });

    table.style.display = 'none';
    empty.style.display = 'block';
    empty.textContent = 'Введите минимум 4 цифры для поиска';
</script>
</body>
</html>
"""


@app.get("/", response_class=HTMLResponse)
async def index():
    return HTML_PAGE


@app.get("/api/shipments")
async def api_shipments(q: str = Query("", description="Строка поиска (минимум 4 цифры)")):
    digits = re.sub(r"\D", "", q)
    if len(digits) < 4:
        return JSONResponse(content=[])

    suffix = digits[-4:]

    conn = await asyncpg.connect(DATABASE_URL)
    try:
        rows = await conn.fetch("""
            SELECT order_id, direction, address, status
            FROM shipments
            WHERE order_id LIKE $1
            ORDER BY ship_date DESC
            LIMIT 50
        """, f"%{suffix}%")
    finally:
        await conn.close()

    return JSONResponse(content=[dict(r) for r in rows])


if __name__ == "__main__":
    asyncio.run(main())
