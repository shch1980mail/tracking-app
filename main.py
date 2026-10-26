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
app = FastAPI(title="Tracking App")

# ============================================================
# БАЗА ДАННЫХ И ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ   
# ============================================================

async def mark_as_done(conn, order_id):
    await conn.execute("""
        UPDATE shipments
        SET status = 'завершено', current_lat = NULL, current_lng = NULL
        WHERE order_id = $1
    """, order_id)

async def save_position(conn, order_id, lat, lng):
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

# ============================================================
# ПАРСЕРЫ GPS
# ============================================================

async def get_sid_from_maps_baltgps(gps_link: str):
    captured = {"sid": None}
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context()
        page = await context.new_page()

        def extract_sid(url):
            if "sid=" not in url: return None
            m = re.search(r"[?&]sid=([a-f0-9]{20,})", url)
            return m.group(1) if m else None

        async def on_request(request):
            sid = extract_sid(request.url)
            if sid and not captured["sid"]:
                captured["sid"] = sid

        async def on_response(response):
            sid = extract_sid(response.url)
            if sid and not captured["sid"]:
                captured["sid"] = sid

        page.on("request", on_request)
        page.on("response", on_response)

        try:
            await page.goto(gps_link, timeout=60000, wait_until="domcontentloaded")
            await page.wait_for_timeout(10000)
        except Exception as e:
            print(f"Playwright error: {e}")
        finally:
            await browser.close()
    return captured["sid"]

async def get_coords_from_gs_baltgps(gps_link: str):
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        try:
            await page.goto(gps_link, timeout=90000, wait_until="domcontentloaded")
            await page.wait_for_timeout(20000)
            html = await page.content()
            m = re.findall(r'N(\d+\.\d+)[,\s]*E(\d+\.\d+)', html)
            if m:
                return float(m[-1][0]), float(m[-1][1])
            else:
                await page.mouse.click(900, 400)
                await page.wait_for_timeout(3000)
                html = await page.content()
                m = re.findall(r'N(\d+\.\d+)[,\s]*E(\d+\.\d+)', html)
                if m:
                    return float(m[-1][0]), float(m[-1][1])
        except Exception as e:
            print(f"Playwright error (gs): {e}")
        finally:
            await browser.close()
    return None, None

async def get_coords_from_axenta(gps_link: str):
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        try:
            await page.goto(gps_link, timeout=90000, wait_until="domcontentloaded")
            await page.wait_for_timeout(20000)
            html = await page.content()
            m = re.findall(r'"lat"\s*:\s*(\d+\.\d+).*?"lng"\s*:\s*(\d+\.\d+)', html)
            if m:
                return float(m[0][0]), float(m[0][1])
        except Exception as e:
            print(f"Playwright error (axenta): {e}")
        finally:
            await browser.close()
    return None, None

def get_units_from_token(token: str):
    try:
        r = requests.get(
            "https://maps.baltgps.ru/wialon/ajax.html",
            params={"svc": "token/login", "params": json.dumps({"token": token})},
            timeout=15
        )
        data = r.json()
        if "token" in data:
            return json.loads(data["token"]).get("items", [])
    except Exception as e:
        print(f"token/login error: {e}")
    return []

def get_position_by_sid(sid: str, unit_id: int):
    try:
        r = requests.get(
            "https://maps.baltgps.ru/wialon/ajax.html",
            params={"svc": "core/search_item", "params": json.dumps({"id": unit_id, "flags": 1025}), "sid": sid},
            timeout=10
        )
        data = r.json()
        if "error" in data: return None, None
        item = data.get("item", {})
        pos = item.get("pos")
        if pos and "x" in pos and "y" in pos:
            return pos["y"], pos["x"]
    except Exception as e:
        print(f"search_item error: {e}")
    return None, None

async def process_order(conn, order_id: str, gps_link: str):
    if not gps_link or not gps_link.startswith("http"): return False

    if "axenta.cloud" in gps_link:
        lat, lng = await get_coords_from_axenta(gps_link)
        if lat and lng:
            await save_position(conn, order_id, lat, lng)
            return True
        await mark_as_done(conn, order_id)
        return False

    if "gs.baltgps.ru" in gps_link:
        lat, lng = await get_coords_from_gs_baltgps(gps_link)
        if lat and lng:
            await save_position(conn, order_id, lat, lng)
            return True
        await mark_as_done(conn, order_id)
        return False

    if "maps.baltgps.ru" in gps_link:
        token = gps_link.split("?t=")[-1]
        units = get_units_from_token(token)
        if not units:
            await mark_as_done(conn, order_id)
            return False

        sid = await get_sid_from_maps_baltgps(gps_link)
        if not sid:
            await mark_as_done(conn, order_id)
            return False

        lat, lng = get_position_by_sid(sid, units[0])
        if lat and lng:
            await save_position(conn, order_id, lat, lng)
            return True
        await mark_as_done(conn, order_id)
        return False
    return False

async def background_parser():
    while True:
        try:
            conn = await asyncpg.connect(DATABASE_URL)
            rows = await conn.fetch("""
                SELECT order_id, gps_link FROM shipments
                WHERE status IN ('в пути', 'планируется') 
                  AND gps_link IS NOT NULL AND gps_link != ''
            """)
            print(f"Найдено {len(rows)} активных заказов")
            for row in rows:
                try:
                    await process_order(conn, row["order_id"], row["gps_link"])
                except Exception as e:
                    print(f"Error {row['order_id']}: {e}")
            await conn.close()
        except Exception as e:
            print(f"DB Error: {e}")
        await asyncio.sleep(300) # Каждые 5 минут

@app.on_event("startup")
async def startup_event():
    asyncio.create_task(background_parser())

# ============================================================
# ВЕБ-ИНТЕРФЕЙС (НОВЫЙ ДИЗАЙН С ТЕХНОЛОГИЧНЫМ БАННЕРОМ)
# ============================================================

HTML_PAGE = """
<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>ТПК — Отслеживание</title>
    <style>
        :root {
            --bg: #f3f4f6; --card: #ffffff; --text: #1f2937; --text-muted: #6b7280;
            --primary: #2563eb; --primary-light: #eff6ff; --border: #e5e7eb;
            --success: #10b981; --success-bg: #d1fae5;
            --warning: #f59e0b; --warning-bg: #fef3c7;
        }
        [data-theme="dark"] {
            --bg: #111827; --card: #1f2937; --text: #f9fafb; --text-muted: #9ca3af;
            --primary: #3b82f6; --primary-light: #1e3a8a; --border: #374151;
            --success: #34d399; --success-bg: #064e3b;
            --warning: #fbbf24; --warning-bg: #78350f;
        }
        * { box-sizing: border-box; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
            background: var(--bg); color: var(--text); margin: 0; padding: 0;
            transition: background 0.3s, color 0.3s;
        }
        
        /* --- Технологичный баннер (карта маршрутов) --- */
        .hero {
            position: relative;
            background-color: #0f172a;
            background-image: 
                radial-gradient(circle at 15% 50%, rgba(59, 130, 246, 0.15) 0%, transparent 50%),
                radial-gradient(circle at 85% 30%, rgba(16, 185, 129, 0.15) 0%, transparent 50%),
                linear-gradient(rgba(255,255,255,0.03) 1px, transparent 1px),
                linear-gradient(90deg, rgba(255,255,255,0.03) 1px, transparent 1px);
            background-size: 100% 100%, 100% 100%, 30px 30px, 30px 30px;
            padding: 50px 24px;
            color: white;
            margin-bottom: 24px;
            overflow: hidden;
            border-bottom: 2px solid var(--primary);
        }
        /* Анимированная линия маршрута на фоне */
        .hero::after {
            content: '';
            position: absolute;
            top: 50%; left: -100%;
            width: 200%; height: 2px;
            background: linear-gradient(90deg, transparent, var(--primary), transparent);
            animation: scan 4s linear infinite;
            opacity: 0.5;
        }
        @keyframes scan {
            0% { transform: translateX(0); }
            100% { transform: translateX(100%); }
        }
        .hero-content {
            position: relative;
            z-index: 2;
            max-width: 1400px;
            margin: 0 auto;
            display: flex;
            justify-content: space-between;
            align-items: center;
        }
        .hero h1 { font-size: 28px; margin: 0; display: flex; align-items: center; gap: 12px; font-weight: 700; text-shadow: 0 2px 4px rgba(0,0,0,0.5); }
        .hero p { margin: 8px 0 0 0; color: #94a3b8; font-size: 15px; }

        .container { max-width: 1400px; margin: 0 auto; padding: 0 24px 40px 24px; }
        
        .theme-toggle { 
            background: rgba(255,255,255,0.1); color: white; border: 1px solid rgba(255,255,255,0.2); 
            font-size: 20px; cursor: pointer; padding: 8px 12px; border-radius: 8px; backdrop-filter: blur(4px);
            transition: background 0.2s;
        }
        .theme-toggle:hover { background: rgba(255,255,255,0.2); }

        /* --- Табы --- */
        .tabs { display: flex; gap: 12px; margin-bottom: 24px; background: var(--card); padding: 8px; border-radius: 12px; border: 1px solid var(--border); }
        .tab { flex: 1; text-align: center; padding: 12px; border-radius: 8px; cursor: pointer; font-weight: 500; color: var(--text-muted); transition: all 0.2s; }
        .tab.active { background: var(--primary); color: white; }
        .tab .count { background: rgba(0,0,0,0.1); padding: 2px 8px; border-radius: 99px; font-size: 12px; margin-left: 8px; }
        .tab.active .count { background: rgba(255,255,255,0.2); }

        /* --- Панель поиска --- */
        .controls { display: flex; gap: 16px; margin-bottom: 24px; }
        .search-wrap { position: relative; flex: 1; }
        .search-wrap input { width: 100%; padding: 14px 20px 14px 48px; font-size: 16px; border: 1px solid var(--border); border-radius: 12px; background: var(--card); color: var(--text); outline: none; box-shadow: 0 1px 2px rgba(0,0,0,0.05); transition: border-color 0.2s; }
        .search-wrap input:focus { border-color: var(--primary); box-shadow: 0 0 0 3px var(--primary-light); }
        .search-icon { position: absolute; left: 16px; top: 50%; transform: translateY(-50%); color: var(--text-muted); pointer-events: none; }
        .sort-wrap select { height: 100%; padding: 0 16px; font-size: 14px; border: 1px solid var(--border); border-radius: 12px; background: var(--card); color: var(--text); outline: none; cursor: pointer; }

        /* --- Карточки --- */
        .grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(320px, 1fr)); gap: 16px; }
        .card {
            background: var(--card); border-radius: 12px; padding: 20px;
            border: 1px solid var(--border); border-left: 4px solid var(--primary);
            cursor: pointer; transition: transform 0.1s, box-shadow 0.1s; position: relative;
        }
        .card:hover { transform: translateY(-2px); box-shadow: 0 4px 12px rgba(0,0,0,0.05); }
        .card-header { display: flex; justify-content: space-between; align-items: flex-start; margin-bottom: 16px; }
        .card-title { font-size: 18px; font-weight: 700; color: var(--primary); }
        
        .status-badge { padding: 4px 10px; border-radius: 99px; font-size: 12px; font-weight: 600; text-transform: uppercase; }
        .status-badge.transit { background: var(--success-bg); color: var(--success); }
        .status-badge.pending { background: var(--warning-bg); color: var(--warning); }
        
        .card-row { display: flex; margin-bottom: 8px; font-size: 14px; }
        .card-label { width: 110px; color: var(--text-muted); flex-shrink: 0; }
        .card-value { font-weight: 500; word-break: break-word; }

        /* --- Модальное окно и карта --- */
        .modal-overlay { position: fixed; top: 0; left: 0; right: 0; bottom: 0; background: rgba(0,0,0,0.5); display: none; justify-content: center; align-items: center; z-index: 1000; opacity: 0; transition: opacity 0.2s; }
        .modal-overlay.active { display: flex; opacity: 1; }
        .modal-content { background: var(--card); width: 100%; max-width: 800px; border-radius: 16px; overflow: hidden; display: flex; flex-direction: column; max-height: 90vh; box-shadow: 0 20px 25px -5px rgba(0,0,0,0.1); }
        .modal-header { padding: 20px; border-bottom: 1px solid var(--border); display: flex; justify-content: space-between; align-items: center; }
        .modal-title { font-size: 20px; font-weight: 700; margin: 0; }
        .modal-close { background: none; border: none; font-size: 24px; cursor: pointer; color: var(--text-muted); }
        .modal-body { padding: 20px; overflow-y: auto; }
        
        .modal-info-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; margin-bottom: 24px; }
        .info-block { display: flex; flex-direction: column; gap: 4px; }
        .info-label { font-size: 12px; color: var(--text-muted); text-transform: uppercase; letter-spacing: 0.5px; }
        .info-val { font-size: 15px; font-weight: 500; }
        
        #map-container { width: 100%; height: 400px; background: var(--border); border-radius: 12px; overflow: hidden; }
        .no-map { display: flex; align-items: center; justify-content: center; height: 100%; color: var(--text-muted); font-size: 14px; }
        .empty { text-align: center; padding: 40px; color: var(--text-muted); grid-column: 1 / -1; }
    </style>
</head>
<body>
    <div class="hero">
        <div class="hero-content">
            <div>
                <h1>🛰️ ТПК — Отслеживание</h1>
                <p>Система спутникового мониторинга и контроля логистики</p>
            </div>
            <button class="theme-toggle" onclick="toggleTheme()">🌙</button>
        </div>
    </div>

    <div class="container">
        <div class="tabs" id="tabs">
            <div class="tab active" data-status="в пути">🚚 В пути <span class="count" id="count-transit">0</span></div>
            <div class="tab" data-status="планируется">📅 Планируется <span class="count" id="count-pending">0</span></div>
            <div class="tab" data-status="завершено">✅ Завершено <span class="count" id="count-done">0</span></div>
        </div>

        <div class="controls">
            <div class="search-wrap">
                <svg class="search-icon" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                    <circle cx="11" cy="11" r="8"></circle>
                    <line x1="21" y1="21" x2="16.65" y2="16.65"></line>
                </svg>
                <input id="search" type="text" placeholder="Поиск по номеру, адресу или машине..." autocomplete="off">
            </div>
            <div class="sort-wrap">
                <select id="sort">
                    <option value="arrival">По дате прибытия</option>
                    <option value="order">По номеру заказа</option>
                </select>
            </div>
        </div>

        <div class="grid" id="grid"></div>
    </div>

    <!-- Модальное окно -->
    <div class="modal-overlay" id="modal" onclick="closeModal(event)">
        <div class="modal-content" onclick="event.stopPropagation()">
            <div class="modal-header">
                <h2 class="modal-title" id="modal-title">Заказ</h2>
                <button class="modal-close" onclick="closeModal()">×</button>
            </div>
            <div class="modal-body">
                <div class="modal-info-grid" id="modal-info"></div>
                <div id="map-container"></div>
            </div>
        </div>
    </div>

<script>
    let currentStatus = 'в пути';
    let allData = [];
    
    // Тема
    const isDark = localStorage.getItem('theme') === 'dark';
    if (isDark) document.documentElement.setAttribute('data-theme', 'dark');
    document.querySelector('.theme-toggle').innerText = isDark ? '☀️' : '🌙';

    function toggleTheme() {
        const root = document.documentElement;
        const isDarkNow = root.getAttribute('data-theme') === 'dark';
        if (isDarkNow) {
            root.removeAttribute('data-theme');
            localStorage.setItem('theme', 'light');
            document.querySelector('.theme-toggle').innerText = '🌙';
        } else {
            root.setAttribute('data-theme', 'dark');
            localStorage.setItem('theme', 'dark');
            document.querySelector('.theme-toggle').innerText = '☀️';
        }
    }

    // Загрузка данных
    async function loadData() {
        try {
            const res = await fetch('/api/shipments');
            allData = await res.json();
            updateCounts();
            render();
        } catch (e) {
            console.error("Ошибка загрузки:", e);
        }
    }

    function updateCounts() {
        document.getElementById('count-transit').innerText = allData.filter(d => d.status === 'в пути').length;
        document.getElementById('count-pending').innerText = allData.filter(d => d.status === 'планируется').length;
        document.getElementById('count-done').innerText = allData.filter(d => d.status === 'завершено').length;
    }

    function statusClass(status) {
        if (status === 'в пути') return 'transit';
        if (status === 'планируется') return 'pending';
        return 'done';
    }

    function render() {
        const query = document.getElementById('search').value.toLowerCase().trim();
        const sortBy = document.getElementById('sort').value;
        const grid = document.getElementById('grid');
        
        let filtered = allData;
        
        // 1. Фильтр по статусу (табы)
        if (currentStatus) {
            filtered = filtered.filter(d => d.status === currentStatus);
        }
        
        // 2. Поиск
        if (query) {
            filtered = filtered.filter(d => 
                (d.order_id && d.order_id.toLowerCase().includes(query)) || 
                (d.address && d.address.toLowerCase().includes(query)) ||
                (d.vehicle && d.vehicle.toLowerCase().includes(query))
            );
        }

        // 3. Сортировка
        filtered.sort((a, b) => {
            if (sortBy === 'arrival') {
                const dateA = a.plan_arrival ? new Date(a.plan_arrival).getTime() : Infinity;
                const dateB = b.plan_arrival ? new Date(b.plan_arrival).getTime() : Infinity;
                return dateA - dateB;
            } else {
                return (a.order_id || '').localeCompare(b.order_id || '');
            }
        });

        if (!filtered.length) {
            grid.innerHTML = '<div class="empty">Ничего не найдено</div>';
            return;
        }

        grid.innerHTML = filtered.map(item => `
            <div class="card" onclick="openModal('${item.order_id}')">
                <div class="card-header">
                    <div class="card-title">${item.order_id}</div>
                    <div class="status-badge ${statusClass(item.status)}">${item.status}</div>
                </div>
                <div class="card-row"><div class="card-label">Адрес:</div><div class="card-value">${item.address || '—'}</div></div>
                <div class="card-row"><div class="card-label">Перевозчик:</div><div class="card-value">${item.carrier || '—'}</div></div>
                <div class="card-row"><div class="card-label">Машина:</div><div class="card-value">${item.vehicle || '—'}</div></div>
                <div class="card-row"><div class="card-label">Прибытие:</div><div class="card-value">${item.plan_arrival || '—'}</div></div>
                <div class="card-row"><div class="card-label">Примечание:</div><div class="card-value">${item.comment || '—'}</div></div>
            </div>
        `).join('');
    }

    document.getElementById('tabs').addEventListener('click', (e) => {
        const tab = e.target.closest('.tab');
        if (!tab) return;
        document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
        tab.classList.add('active');
        currentStatus = tab.dataset.status;
        render();
    });

    document.getElementById('search').addEventListener('input', render);
    document.getElementById('sort').addEventListener('change', render);

    // Модальное окно
    function openModal(orderId) {
        const item = allData.find(d => d.order_id === orderId);
        if (!item) return;

        document.getElementById('modal-title').innerText = item.order_id;
        document.getElementById('modal-info').innerHTML = `
            <div class="info-block"><div class="info-label">Адрес доставки</div><div class="info-val">${item.address || '—'}</div></div>
            <div class="info-block"><div class="info-label">Перевозчик</div><div class="info-val">${item.carrier || '—'}</div></div>
            <div class="info-block"><div class="info-label">Машина</div><div class="info-val">${item.vehicle || '—'}</div></div>
            <div class="info-block"><div class="info-label">Водитель</div><div class="info-val">${item.driver || '—'}</div></div>
            <div class="info-block"><div class="info-label">Статус</div><div class="info-val">${item.status}</div></div>
            <div class="info-block"><div class="info-label">План. прибытие</div><div class="info-val">${item.plan_arrival || '—'}</div></div>
            <div class="info-block" style="grid-column: span 2;"><div class="info-label">Примечание</div><div class="info-val">${item.comment || '—'}</div></div>
        `;

        const mapContainer = document.getElementById('map-container');
        mapContainer.innerHTML = '';

        if (item.current_lat && item.current_lng) {
            const lat = item.current_lat;
            const lng = item.current_lng;
            const mapUrl = `https://yandex.ru/map-widget/v1/?ll=${lng}%2C${lat}&z=12&pt=${lng}%2C${lat}%2Cpm2rdm`;
            mapContainer.innerHTML = `<iframe src="${mapUrl}" width="100%" height="100%" frameborder="0" allowfullscreen="true"></iframe>`;
        } else {
            mapContainer.innerHTML = '<div class="no-map">Координаты еще не получены (машина в пути или нет GPS-ссылки)</div>';
        }

        document.getElementById('modal').classList.add('active');
    }

    function closeModal(e) {
        if (e && e.target !== e.currentTarget) return;
        document.getElementById('modal').classList.remove('active');
    }

    // Инициализация
    loadData();
    setInterval(loadData, 60000);
</script>
</body>
</html>
"""

# ============================================================
# ЭНДПОИНТЫ API
# ============================================================

@app.get("/", response_class=HTMLResponse)
async def index():
    return HTML_PAGE

@app.get("/api/shipments")
async def api_shipments():
    """Возвращает все заказы для отрисовки."""
    conn = await asyncpg.connect(DATABASE_URL)
    try:
        rows = await conn.fetch("""
            SELECT order_id, direction, address, carrier, vehicle, driver, 
                   status, plan_arrival::text, comment, current_lat, current_lng 
            FROM shipments 
            ORDER BY 
                CASE status 
                    WHEN 'в пути' THEN 1 
                    WHEN 'планируется' THEN 2 
                    ELSE 3 
                END,
                ship_date DESC 
            LIMIT 200
        """)
    finally:
        await conn.close()
    return JSONResponse(content=[dict(r) for r in rows])
