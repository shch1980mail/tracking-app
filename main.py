from fastapi import FastAPI, HTTPException, Depends
from fastapi.responses import HTMLResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel
from typing import Optional
import os, json, asyncpg
from datetime import date, datetime, timedelta
import urllib.request
import urllib.parse
import httpx

app = FastAPI(title="ТПК Отслеживание")
security = HTTPBasic()

DATABASE_URL = os.environ.get("DATABASE_URL")
ADMIN_USER = os.environ.get("ADMIN_USER", "admin")
ADMIN_PASS = os.environ.get("ADMIN_PASS", "admin123")
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN")

if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL environment variable must be set")

pool = None
last_trigger = None


class Shipment(BaseModel):
    order_id: str
    direction: Optional[str] = None
    address: Optional[str] = None
    weight: Optional[float] = None
    ship_date: Optional[str] = None
    plan_arrival: Optional[str] = None
    carrier: Optional[str] = None
    vehicle: Optional[str] = None
    driver: Optional[str] = None
    gps_link: Optional[str] = None
    status: Optional[str] = "планируется"


def parse_date(v):
    if not v:
        return None
    try:
        return date.fromisoformat(str(v)[:10])
    except Exception:
        return None


def geocode(address):
    if not address:
        return None, None
    try:
        url = "https://nominatim.openstreetmap.org/search?" + urllib.parse.urlencode({
            "q": address, "format": "json", "limit": 1
        })
        req = urllib.request.Request(url, headers={"User-Agent": "tpk-tracking/1.0"})
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read())
        if data:
            return float(data[0]["lat"]), float(data[0]["lon"])
    except Exception as e:
        print(f"geocode error: {e}")
    return None, None


def calc_route(lat1, lng1, lat2, lng2):
    try:
        url = f"http://router.project-osrm.org/route/v1/driving/{lng1},{lat1};{lng2},{lat2}?overview=false"
        with urllib.request.urlopen(url, timeout=15) as r:
            data = json.loads(r.read())
        if data.get("routes"):
            return round(data["routes"][0]["distance"] / 1000)
    except Exception as e:
        print(f"route error: {e}")
    return None


def calc_eta(current_lat, current_lng, dest_lat, dest_lng,
             ship_lat, ship_lng, ship_date):
    """Реалистичная ETA: учитывает пройденный путь и фактическую скорость."""
    remaining = calc_route(current_lat, current_lng, dest_lat, dest_lng)
    if not remaining:
        return None, None, None, None

    speed = 500.0  # км/день по умолчанию

    if ship_lat and ship_lng and ship_date:
        passed = calc_route(ship_lat, ship_lng, current_lat, current_lng)

        # Преобразуем ship_date в datetime, если пришёл date
        if isinstance(ship_date, date) and not isinstance(ship_date, datetime):
            ship_dt = datetime.combine(ship_date, datetime.min.time())
        else:
            ship_dt = ship_date

        days_in_transit = (datetime.now() - ship_dt).total_seconds() / 86400

        if passed and passed >= 50 and days_in_transit >= 0.5:
            real_speed = passed / days_in_transit
            speed = max(250, min(800, real_speed))
            print(f"ETA: пройдено {passed} км за {days_in_transit:.1f} дн → скорость {real_speed:.0f} км/день")

    eta_days = remaining / speed
    eta_date = datetime.now() + timedelta(days=eta_days)
    return remaining, round(eta_days, 1), eta_date.strftime("%d.%m.%Y"), round(speed)


async def trigger_parser():
    global last_trigger
    if not GITHUB_TOKEN:
        return False
    now = datetime.now()
    if last_trigger and (now - last_trigger).total_seconds() < 120:
        return False
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                "https://api.github.com/repos/shch1980mail/tracking-app/actions/workflows/parser.yml/dispatches",
                headers={
                    "Authorization": f"Bearer {GITHUB_TOKEN}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28"
                },
                json={"ref": "main"},
                timeout=10
            )
            if resp.status_code in (204, 200):
                last_trigger = now
                return True
    except Exception as e:
        print(f"trigger error: {e}")
    return False


@app.on_event("startup")
async def startup():
    global pool
    pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5)
    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS shipments (
                order_id VARCHAR(50) PRIMARY KEY,
                direction VARCHAR(100),
                address TEXT,
                weight DECIMAL,
                ship_date DATE,
                plan_arrival DATE,
                carrier VARCHAR(150),
                vehicle VARCHAR(150),
                driver TEXT,
                gps_link TEXT,
                status VARCHAR(30) DEFAULT 'планируется',
                current_lat DECIMAL(10,6),
                current_lng DECIMAL(10,6),
                ship_lat DECIMAL(10,6),
                ship_lng DECIMAL(10,6),
                first_seen_at TIMESTAMP,
                last_position_update TIMESTAMP,
                remaining_km INTEGER,
                eta_date DATE,
                actual_speed DECIMAL(6,2),
                created_at TIMESTAMP DEFAULT NOW(),
                updated_at TIMESTAMP DEFAULT NOW()
            )
        """)
        for stmt in [
            "ALTER TABLE shipments ADD COLUMN IF NOT EXISTS ship_lat DECIMAL(10,6)",
            "ALTER TABLE shipments ADD COLUMN IF NOT EXISTS ship_lng DECIMAL(10,6)",
            "ALTER TABLE shipments ADD COLUMN IF NOT EXISTS first_seen_at TIMESTAMP",
            "ALTER TABLE shipments ADD COLUMN IF NOT EXISTS actual_speed DECIMAL(6,2)",
        ]:
            try:
                await conn.execute(stmt)
            except Exception as e:
                print(f"ALTER: {e}")

        if os.path.exists("shipments.json"):
            with open("shipments.json", "r", encoding="utf-8") as f:
                seed = json.load(f)
            print(f"Начинаю загрузку {len(seed)} заказов...")
            loaded = 0
            for order_id, s in seed.items():
                try:
                    await conn.execute("""
                        INSERT INTO shipments (order_id, direction, address, weight,
                            ship_date, plan_arrival, carrier, vehicle, driver, gps_link, status)
                        VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)
                        ON CONFLICT (order_id) DO UPDATE SET
                            direction=EXCLUDED.direction,
                            address=EXCLUDED.address,
                            weight=EXCLUDED.weight,
                            ship_date=EXCLUDED.ship_date,
                            plan_arrival=EXCLUDED.plan_arrival,
                            carrier=EXCLUDED.carrier,
                            vehicle=EXCLUDED.vehicle,
                            driver=EXCLUDED.driver,
                            gps_link=EXCLUDED.gps_link,
                            status=EXCLUDED.status,
                            updated_at=NOW()
                    """, s.get("order_id"), s.get("direction"), s.get("address"),
                        s.get("weight"), parse_date(s.get("ship_date")),
                        parse_date(s.get("plan_arrival")),
                        s.get("carrier"), s.get("vehicle"), s.get("driver"),
                        s.get("gps_link"), s.get("status"))
                    loaded += 1
                except Exception as e:
                    print(f"Ошибка вставки {order_id}: {e}")
            print(f"Загружено {loaded} заказов в базу")


@app.on_event("shutdown")
async def shutdown():
    if pool:
        await pool.close()


def check_admin(credentials: HTTPBasicCredentials = Depends(security)):
    if credentials.username != ADMIN_USER or credentials.password != ADMIN_PASS:
        raise HTTPException(401, "Неверный логин/пароль")
    return credentials.username


@app.get("/api/trackable")
async def trackable_orders():
    """Список заказов с GPS-ссылкой для выпадающего меню на главной."""
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT order_id, address, carrier, vehicle, status,
                   current_lat, current_lng, last_position_update, eta_date
            FROM shipments
            WHERE gps_link IS NOT NULL 
              AND gps_link != '' 
              AND status IN ('в пути', 'планируется')
            ORDER BY 
              CASE status WHEN 'в пути' THEN 1 WHEN 'планируется' THEN 2 ELSE 3 END,
              order_id DESC
        """)
    return [dict(r) for r in rows]


@app.get("/api/track/{order_id}")
async def track_order(order_id: str):
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM shipments WHERE order_id = $1", order_id)
    if not row:
        raise HTTPException(404, f"Заказ {order_id} не найден")

    data = dict(row)
    is_stale = True
    if data.get("last_position_update"):
        age = (datetime.now() - data["last_position_update"]).total_seconds()
        is_stale = age > 3600

    if data.get("gps_link") and data.get("status") == "в пути" and is_stale:
        await trigger_parser()

    if data.get("current_lat") and data.get("address"):
        dest_lat, dest_lng = geocode(data["address"])
        if dest_lat and dest_lng:
            remaining, eta_days, eta_date, speed = calc_eta(
                float(data["current_lat"]),
                float(data["current_lng"]),
                dest_lat, dest_lng,
                float(data["ship_lat"]) if data.get("ship_lat") else None,
                float(data["ship_lng"]) if data.get("ship_lng") else None,
                data.get("ship_date"),
            )
            if remaining:
                data["remaining_km"] = remaining
                data["eta_days"] = eta_days
                data["eta_date"] = eta_date
                data["actual_speed"] = speed

                async with pool.acquire() as conn2:
                    await conn2.execute("""
                        UPDATE shipments
                        SET remaining_km = $1, eta_date = $2, actual_speed = $3
                        WHERE order_id = $4
                    """, remaining,
                        parse_date(datetime.strptime(eta_date, "%d.%m.%Y").strftime("%Y-%m-%d")),
                        speed, order_id)

    return data


@app.post("/api/admin/refresh/all")
async def admin_refresh_all(user=Depends(check_admin)):
    ok = await trigger_parser()
    return {"ok": ok, "message": "Парсер запущен" if ok else "Не удалось (антифлуд или нет токена)"}


@app.get("/api/admin/shipments")
async def admin_list(user=Depends(check_admin)):
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT * FROM shipments ORDER BY order_id DESC")
    return [dict(r) for r in rows]


@app.post("/api/admin/shipments")
async def admin_create(s: Shipment, user=Depends(check_admin)):
    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO shipments (order_id, direction, address, weight, ship_date,
                plan_arrival, carrier, vehicle, driver, gps_link, status)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)
            ON CONFLICT (order_id) DO UPDATE SET
                direction=EXCLUDED.direction, address=EXCLUDED.address,
                weight=EXCLUDED.weight, ship_date=EXCLUDED.ship_date,
                plan_arrival=EXCLUDED.plan_arrival, carrier=EXCLUDED.carrier,
                vehicle=EXCLUDED.vehicle, driver=EXCLUDED.driver,
                gps_link=EXCLUDED.gps_link, status=EXCLUDED.status,
                updated_at=NOW()
        """, s.order_id, s.direction, s.address, s.weight,
            parse_date(s.ship_date), parse_date(s.plan_arrival),
            s.carrier, s.vehicle, s.driver, s.gps_link, s.status)
    return {"ok": True}


@app.delete("/api/admin/shipments/{order_id}")
async def admin_delete(order_id: str, user=Depends(check_admin)):
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM shipments WHERE order_id = $1", order_id)
    return {"ok": True}


@app.get("/", response_class=HTMLResponse)
async def user_portal():
    return """
    <!DOCTYPE html>
    <html>
    <head>
        <title>Отслеживание заказа</title>
        <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
        <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
        <style>
            body { font-family: Arial; padding: 40px; background: #f0f2f5; margin: 0; }
            .card { max-width: 900px; margin: 0 auto; background: white; padding: 30px; border-radius: 10px; box-shadow: 0 2px 8px rgba(0,0,0,0.1); }
            label { display:block; font-weight:bold; color:#555; margin-bottom:6px; font-size:14px; }
            select { padding:12px; width:100%; border:1px solid #ccc; border-radius:4px; font-size:15px; box-sizing:border-box; background:white; cursor:pointer; }
            select:focus { outline: none; border-color:#007bff; }
            button { padding:12px 24px; background:#007bff; color:white; border:none; border-radius:4px; cursor:pointer; font-size:16px; margin-top:10px; }
            button:hover { background:#0056b3; }
            #result { margin-top:20px; }
            #map { height: 500px; margin-top: 20px; border-radius: 8px; display:none; }
            .info-row { margin: 8px 0; font-size: 15px; }
            .info-row b { display:inline-block; min-width:180px; color: #555; }
            .status-badge { display:inline-block; padding:4px 12px; border-radius:12px; font-size:13px; font-weight:bold; }
            .status-в-пути { background:#d4edda; color:#155724; }
            .status-завершено { background:#e2e3e5; color:#383d41; }
            .status-планируется { background:#fff3cd; color:#856404; }
            .updating { color: #856404; background: #fff3cd; padding: 8px 12px; border-radius: 6px; margin-top: 10px; }
            .no-orders { color: #888; padding: 15px; background: #f8f9fa; border-radius: 6px; text-align:center; }
            .counter { color: #6c757d; font-size: 13px; margin-top: 5px; }
        </style>
    </head>
    <body>
        <div class="card">
            <h1>📍 Отслеживание заказа</h1>
            <label for="orderSelect">Выберите заказ</label>
            <select id="orderSelect" onchange="track()">
                <option value="">— Загрузка списка... —</option>
            </select>
            <div class="counter" id="counter"></div>
            <div id="result"></div>
            <div id="map"></div>
        </div>

        <script>
        let map = null;
        let refreshTimer = null;

        async function loadTrackable() {
            try {
                const res = await fetch('/api/trackable');
                const orders = await res.json();
                const select = document.getElementById('orderSelect');

                if (orders.length === 0) {
                    select.innerHTML = '<option value="">Нет заказов с GPS-трекингом</option>';
                    document.getElementById('counter').innerHTML = 
                        '<div class="no-orders">По активным заказам нет GPS-ссылок</div>';
                    return;
                }

                let options = '<option value="">— Выберите заказ —</option>';
                for (const o of orders) {
                    const gpsBadge = o.current_lat ? ' 📍' : ' ⏳';
                    const statusLabel = o.status === 'в пути' ? '🚚' : '📋';
                    const addr = o.address ? ` → ${o.address}` : '';
                    options += `<option value="${o.order_id}">${statusLabel} ${o.order_id}${addr}${gpsBadge}</option>`;
                }
                select.innerHTML = options;

                const inTransit = orders.filter(o => o.status === 'в пути').length;
                const withCoords = orders.filter(o => o.current_lat).length;
                document.getElementById('counter').innerHTML = 
                    `Доступно для отслеживания: <b>${orders.length}</b> ` +
                    `(в пути: ${inTransit}, с координатами: ${withCoords})`;
            } catch (e) {
                document.getElementById('orderSelect').innerHTML = 
                    '<option value="">Ошибка загрузки списка</option>';
            }
        }

        async function track() {
            const order = document.getElementById('orderSelect').value;
            if (!order) {
                document.getElementById('result').innerHTML = '';
                document.getElementById('map').style.display = 'none';
                return;
            }

            document.getElementById('result').innerHTML = '<p>Поиск...</p>';
            document.getElementById('map').style.display = 'none';
            if (refreshTimer) { clearTimeout(refreshTimer); refreshTimer = null; }

            try {
                const res = await fetch(`/api/track/${encodeURIComponent(order)}`);
                const data = await res.json();
                if (!res.ok) {
                    document.getElementById('result').innerHTML = `<p style="color:red">${data.detail}</p>`;
                    return;
                }

                let html = `<h2>📦 ${data.order_id}</h2>`;
                html += `<div class="info-row"><b>Направление:</b> ${data.direction || '—'}</div>`;
                html += `<div class="info-row"><b>Адрес доставки:</b> ${data.address || '—'}</div>`;
                html += `<div class="info-row"><b>Перевозчик:</b> ${data.carrier || '—'}</div>`;
                html += `<div class="info-row"><b>Машина:</b> ${data.vehicle || '—'}</div>`;
                html += `<div class="info-row"><b>Статус:</b> <span class="status-badge status-${(data.status||'').replace(' ','-')}">${data.status || '—'}</span></div>`;
                if (data.remaining_km) html += `<div class="info-row"><b>Осталось:</b> ${data.remaining_km} км</div>`;
                if (data.actual_speed) html += `<div class="info-row"><b>Скорость:</b> ${data.actual_speed} км/день</div>`;
                if (data.eta_date) html += `<div class="info-row"><b>Расчётный срок:</b> ${data.eta_date}</div>`;
                if (data.plan_arrival) html += `<div class="info-row"><b>Плановая дата:</b> ${data.plan_arrival}</div>`;
                if (data.last_position_update) html += `<div class="info-row"><b>Обновлено:</b> ${new Date(data.last_position_update).toLocaleString('ru-RU')}</div>`;
                document.getElementById('result').innerHTML = html;

                if (data.current_lat && data.current_lng) {
                    const lat = parseFloat(data.current_lat);
                    const lng = parseFloat(data.current_lng);
                    document.getElementById('map').style.display = 'block';
                    if (map) map.remove();
                    map = L.map('map').setView([lat, lng], 8);
                    L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', { attribution: '© OpenStreetMap' }).addTo(map);
                    L.marker([lat, lng]).addTo(map).bindPopup(`<b>${data.order_id}</b><br>${data.vehicle || ''}`).openPopup();
                } else if (data.status === 'в пути' && data.gps_link) {
                    document.getElementById('result').innerHTML += 
                        '<div class="updating">⏳ Получаем актуальные координаты. Пожалуйста, подождите 1–2 минуты.</div>';
                    refreshTimer = setTimeout(track, 90000);
                }
            } catch (e) {
                document.getElementById('result').innerHTML = `<p style="color:red">Ошибка: ${e.message}</p>`;
            }
        }

        loadTrackable();
        </script>
    </body>
    </html>
    """


@app.get("/admin", response_class=HTMLResponse)
async def admin_panel(user=Depends(check_admin)):
    return """
    <!DOCTYPE html>
    <html>
    <head>
        <title>Админ-панель</title>
        <style>
            body { font-family: Arial; padding: 20px; background: #f0f2f5; }
            table { border-collapse: collapse; width: 100%; background: white; font-size: 13px; }
            th, td { border: 1px solid #ddd; padding: 6px; text-align: left; }
            th { background: #333; color: white; position: sticky; top: 0; }
            tr:hover { background: #f0f8ff; }
            .в-пути { color: green; font-weight: bold; }
            .завершено { color: gray; }
            .планируется { color: orange; }
            button { padding:6px 12px; margin:2px; cursor:pointer; }
            .btn-add { background:#28a745; color:white; border:none; border-radius:4px; font-size:14px; padding:10px 20px; margin-bottom:10px; }
            .btn-edit { background:#ffc107; border:none; border-radius:4px; }
            .btn-del { background:#dc3545; color:white; border:none; border-radius:4px; }
            .btn-refresh { background:#17a2b8; color:white; border:none; border-radius:4px; }
            .btn-filter { background:#e9ecef; border:1px solid #ccc; border-radius:4px; padding:8px 16px; font-size:14px; cursor:pointer; margin-right:4px; }
            .btn-filter.active { background:#007bff; color:white; border-color:#007bff; }
            #filter { padding:8px; width:300px; margin-bottom:10px; }
            .filters-bar { margin-bottom: 10px; }
            #modal { display:none; position:fixed; top:0; left:0; width:100%; height:100%; background:rgba(0,0,0,0.5); }
            #modalContent { background:white; max-width:600px; margin:80px auto; padding:30px; border-radius:8px; max-height:80vh; overflow-y:auto; }
            #modalContent label { display:block; margin-top:10px; font-weight:bold; }
            #modalContent input, #modalContent select { width:100%; padding:8px; box-sizing:border-box; }
            .modal-buttons { margin-top:20px; text-align:right; }
            .gps-yes { color: green; font-weight:bold; }
            .gps-no { color: #ccc; }
            #toast { display:none; position:fixed; bottom:30px; right:30px; background:#333; color:white; padding:15px 25px; border-radius:8px; z-index:9999; }
        </style>
    </head>
    <body>
        <h1>🚚 Админ-панель — Отгрузки</h1>
        <div id="stats"></div>

        <div class="filters-bar">
            <button class="btn-filter active" data-filter="all" onclick="setFilter('all')">Все заказы</button>
            <button class="btn-filter" data-filter="with-gps" onclick="setFilter('with-gps')">📍 С GPS-ссылкой</button>
            <button class="btn-filter" data-filter="with-coords" onclick="setFilter('with-coords')">🎯 С координатами</button>
            <button class="btn-filter" data-filter="in-transit" onclick="setFilter('in-transit')">🚚 В пути</button>
            <button class="btn-filter" data-filter="in-transit-gps" onclick="setFilter('in-transit-gps')">🔥 В пути + GPS</button>
        </div>

        <button class="btn-add" onclick="openAdd()">➕ Добавить заказ</button>
        <button class="btn-refresh" onclick="refreshAll()">🔄 Обновить позиции</button>
        <input id="filter" placeholder="Поиск по номеру или адресу..." oninput="renderTable()"/>

        <table id="shipments">
            <thead>
                <tr>
                    <th>Заказ</th><th>Адрес</th><th>Перевозчик</th>
                    <th>Машина</th><th>Статус</th><th>Позиция</th><th>Скорость</th><th>Действия</th>
                </tr>
            </thead>
            <tbody></tbody>
        </table>

        <div id="toast"></div>

        <div id="modal">
            <div id="modalContent">
                <h2 id="modalTitle">Добавить заказ</h2>
                <label>Номер заказа *</label>
                <input id="f_order_id"/>
                <label>Направление</label>
                <input id="f_direction"/>
                <label>Адрес доставки</label>
                <input id="f_address"/>
                <label>Вес</label>
                <input id="f_weight" type="number"/>
                <label>Дата отгрузки</label>
                <input id="f_ship_date"/>
                <label>Дата прибытия (план)</label>
                <input id="f_plan_arrival"/>
                <label>Перевозчик</label>
                <input id="f_carrier"/>
                <label>Машина</label>
                <input id="f_vehicle"/>
                <label>Водитель</label>
                <input id="f_driver"/>
                <label>GPS-ссылка</label>
                <input id="f_gps_link"/>
                <label>Статус</label>
                <select id="f_status">
                    <option>планируется</option>
                    <option>в пути</option>
                    <option>завершено</option>
                </select>
                <div class="modal-buttons">
                    <button onclick="closeModal()">Отмена</button>
                    <button class="btn-add" onclick="saveOrder()">Сохранить</button>
                </div>
            </div>
        </div>

        <script>
        let allData = [];
        let currentFilter = 'all';

        function showToast(msg) {
            const t = document.getElementById('toast');
            t.innerText = msg;
            t.style.display = 'block';
            setTimeout(() => t.style.display = 'none', 4000);
        }

        function setFilter(filter) {
            currentFilter = filter;
            document.querySelectorAll('.btn-filter').forEach(b => {
                b.classList.toggle('active', b.dataset.filter === filter);
            });
            renderTable();
        }

        async function load() {
            const res = await fetch('/api/admin/shipments');
            allData = await res.json();
            const active = allData.filter(s => s.status === 'в пути').length;
            const withGps = allData.filter(s => s.gps_link && s.gps_link.trim()).length;
            const withCoords = allData.filter(s => s.current_lat).length;
            const inTransitGps = allData.filter(s => s.status === 'в пути' && s.gps_link && s.gps_link.trim()).length;
            document.getElementById('stats').innerHTML =
                `<p>Всего: <b>${allData.length}</b> | 
                 В пути: <b style="color:green">${active}</b> | 
                 С GPS-ссылкой: <b style="color:#6c757d">${withGps}</b> | 
                 С координатами: <b style="color:blue">${withCoords}</b> | 
                 В пути + GPS: <b style="color:#dc3545">${inTransitGps}</b></p>`;
            renderTable();
        }

        function applyFilter(data) {
            return data.filter(s => {
                if (currentFilter === 'with-gps') return s.gps_link && s.gps_link.trim();
                if (currentFilter === 'with-coords') return s.current_lat;
                if (currentFilter === 'in-transit') return s.status === 'в пути';
                if (currentFilter === 'in-transit-gps') return s.status === 'в пути' && s.gps_link && s.gps_link.trim();
                return true;
            });
        }

        function renderTable() {
            const q = document.getElementById('filter').value.toLowerCase();
            let filtered = applyFilter(allData);
            filtered = filtered.filter(s =>
                (s.order_id||'').toLowerCase().includes(q) ||
                (s.address||'').toLowerCase().includes(q)
            );

            if (filtered.length === 0) {
                document.querySelector('#shipments tbody').innerHTML =
                    '<tr><td colspan="8" style="text-align:center;color:#888;padding:20px;">Нет заказов, удовлетворяющих фильтру</td></tr>';
                return;
            }

            document.querySelector('#shipments tbody').innerHTML = filtered.map(s => `
                <tr>
                    <td>${s.order_id}</td>
                    <td>${s.address || ''}</td>
                    <td>${s.carrier || ''}</td>
                    <td>${s.vehicle || ''}</td>
                    <td class="${(s.status||'').replace(' ','-')}">${s.status || ''}</td>
                    <td>${s.current_lat ? '<span class="gps-yes">📍 ' + Number(s.current_lat).toFixed(2) + ', ' + Number(s.current_lng).toFixed(2) + '</span>' : '<span class="gps-no">—</span>'}</td>
                    <td>${s.actual_speed ? Math.round(s.actual_speed) + ' км/д' : '—'}</td>
                    <td>
                        <button class="btn-edit" onclick='openEdit(${JSON.stringify(s).replace(/'/g,"&#39;")})'>✏️</button>
                        <button class="btn-del" onclick="del('${s.order_id}')">🗑</button>
                    </td>
                </tr>
            `).join('');
        }

        async function refreshAll() {
            showToast('Запускаю парсер...');
            const res = await fetch('/api/admin/refresh/all', {method: 'POST'});
            const data = await res.json();
            showToast(data.message || 'Готово');
            setTimeout(load, 120000);
        }

        function openAdd() {
            ['order_id','direction','address','weight','ship_date','plan_arrival','carrier','vehicle','driver','gps_link'].forEach(f => {
                document.getElementById('f_'+f).value = '';
                document.getElementById('f_'+f).disabled = false;
            });
            document.getElementById('f_status').value = 'планируется';
            document.getElementById('modalTitle').innerText = 'Добавить заказ';
            document.getElementById('modal').style.display = 'block';
        }

        function openEdit(s) {
            ['order_id','direction','address','weight','ship_date','plan_arrival','carrier','vehicle','driver','gps_link'].forEach(f => {
                document.getElementById('f_'+f).value = s[f] || '';
            });
            document.getElementById('f_order_id').disabled = true;
            document.getElementById('f_status').value = s.status || 'планируется';
            document.getElementById('modalTitle').innerText = 'Редактировать: ' + s.order_id;
            document.getElementById('modal').style.display = 'block';
        }

        function closeModal() {
            document.getElementById('modal').style.display = 'none';
        }

        async function saveOrder() {
            const payload = {
                order_id: document.getElementById('f_order_id').value.trim(),
                direction: document.getElementById('f_direction').value,
                address: document.getElementById('f_address').value,
                weight: parseFloat(document.getElementById('f_weight').value) || null,
                ship_date: document.getElementById('f_ship_date').value || null,
                plan_arrival: document.getElementById('f_plan_arrival').value || null,
                carrier: document.getElementById('f_carrier').value,
                vehicle: document.getElementById('f_vehicle').value,
                driver: document.getElementById('f_driver').value,
                gps_link: document.getElementById('f_gps_link').value,
                status: document.getElementById('f_status').value
            };
            if (!payload.order_id) { alert('Укажите номер заказа'); return; }

            const res = await fetch('/api/admin/shipments', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify(payload)
            });
            if (res.ok) { closeModal(); load(); showToast('Сохранено'); }
            else { alert('Ошибка сохранения'); }
        }

        async function del(order_id) {
            if (!confirm('Удалить заказ ' + order_id + '?')) return;
            await fetch('/api/admin/shipments/' + order_id, {method: 'DELETE'});
            load();
        }

        load();
        </script>
    </body>
    </html>
    """
