from fastapi import FastAPI, HTTPException, Depends
from fastapi.responses import HTMLResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel
from typing import Optional
import os, json, requests, asyncio

app = FastAPI(title="ТПК Отслеживание")
security = HTTPBasic()

ADMIN_USER = os.environ.get("ADMIN_USER", "admin")
ADMIN_PASS = os.environ.get("ADMIN_PASS", "admin123")

DB = {}

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
    comment: Optional[str] = None
    status: Optional[str] = None

# === ФУНКЦИЯ ПАРСИНГА GPS (С ПРОВЕРКОЙ НА ОШИБКИ) ===
def get_coords_from_baltgps(gps_link: str):
    if not gps_link or "maps.baltgps.ru" not in gps_link:
        return None, None, "no_link"
    try:
        token = gps_link.split("?t=")[-1]
        r = requests.get("https://maps.baltgps.ru/wialon/ajax.html", params={
            "svc": "token/login", "params": json.dumps({"token": token})
        }, timeout=(3.05, 5))
        
        data = r.json()
        
        # Wialon возвращает ошибку, если токен неверный или просрочен
        if "error" in data:
            print(f"❌ Wialon Error: {data['error']}")
            return None, None, "invalid_token"
            
        if "token" not in data: 
            return None, None, "invalid_token"
        
        unit_info = json.loads(data["token"])
        items = unit_info.get("items", [])
        if not items: return None, None, "no_units"
        unit_id = items[0]
        sid = data.get("eid")
        
        r2 = requests.get("https://maps.baltgps.ru/wialon/ajax.html", params={
            "svc": "core/search_item", 
            "params": json.dumps({"id": unit_id, "flags": 1025}), 
            "sid": sid
        }, timeout=(3.05, 5))
        
        pos = r2.json().get("item", {}).get("pos", {})
        if pos and "x" in pos and "y" in pos:
            return float(pos["y"]), float(pos["x"]), "ok"
            
        return None, None, "no_data"
    except Exception as e:
        print(f"Ошибка GPS: {e}")
    return None, None, "error"

async def fetch_single_order_coords(order_id, o):
    loop = asyncio.get_event_loop()
    lat, lng, status = await loop.run_in_executor(None, get_coords_from_baltgps, o["gps_link"])
    
    if lat and lng:
        o["current_lat"] = lat
        o["current_lng"] = lng
        o["gps_status"] = "ok"
    else:
        # Если ссылка невалидна, помечаем это, чтобы на фронте показывать как "без GPS"
        o["gps_status"] = status
        if status in ["invalid_token", "no_link", "error"]:
            o["gps_link"] = None # Убираем ссылку, чтобы не вводить в заблуждение

async def update_all_coords():
    print("⏳ Начинаю фоновое обновление GPS-координат...")
    tasks = []
    for order_id, o in DB.items():
        if o.get("gps_link") and not o.get("current_lat"):
            tasks.append(fetch_single_order_coords(order_id, o))
    
    semaphore = asyncio.Semaphore(10)
    async def limited_task(task):
        async with semaphore:
            await task
            
    await asyncio.gather(*(limited_task(t) for t in tasks))
    print("✅ Фоновое обновление завершено.")

@app.on_event("startup")
async def startup():
    global DB
    if os.path.exists("shipments.json"):
        with open("shipments.json", "r", encoding="utf-8") as f:
            DB = json.load(f)
        print(f"✅ Загружено {len(DB)} заказов")
        asyncio.create_task(update_all_coords())

def check_admin(credentials: HTTPBasicCredentials = Depends(security)):
    if credentials.username != ADMIN_USER or credentials.password != ADMIN_PASS:
        raise HTTPException(401, "Неверный логин/пароль")
    return credentials.username

@app.get("/api/shipments")
async def list_shipments(status: Optional[str] = None, search: Optional[str] = None, sort_by: str = "gps"):
    result = []
    for o in DB.values():
        s = str(o.get("status", "")).lower()
        if status == "in_transit" and s not in ['в пути', 'планируется']: continue
        if status == "delivered" and s not in ['завершено', 'выгружено']: continue
        if status == "no_status" and s != '': continue
        
        # Если ищем "с GPS", показываем только тех, у кого есть ссылка И она валидна
        if status == "with_gps" and not o.get("gps_link"): continue
        
        if search:
            q = search.lower()
            if q not in o.get("order_id", "").lower() and q not in o.get("address", "").lower() and q not in o.get("vehicle", "").lower():
                continue
        result.append(o)
    
    def sort_key(x):
        has_link = 1 if x.get("gps_link") else 0
        has_coords = 1 if x.get("current_lat") else 0
        order_id = str(x.get("order_id") or "")
        city = str(x.get("address") or "").lower()
        arrival = str(x.get("plan_arrival") or "9999-12-31")
        weight = x.get("weight") or 0

        if sort_by == "gps": return (not has_link, not has_coords, order_id)
        elif sort_by == "order_id": return order_id
        elif sort_by == "city": return (city == "", city, order_id)
        elif sort_by == "date": return (arrival == "9999-12-31", arrival, order_id)
        elif sort_by == "weight": return (weight == 0, -weight, order_id)
        
        return (not has_link, not has_coords, order_id)

    result.sort(key=sort_key)
    return result

@app.get("/api/coords/{order_id}")
async def get_online_coords(order_id: str):
    if order_id not in DB:
        raise HTTPException(404, "Заказ не найден")
    o = DB[order_id]
    
    if o.get("current_lat"):
        return {"lat": o["current_lat"], "lng": o["current_lng"]}
        
    if o.get("gps_link"):
        lat, lng, status = get_coords_from_baltgps(o["gps_link"])
        if lat and lng:
            o["current_lat"] = lat
            o["current_lng"] = lng
            return {"lat": lat, "lng": lng}
        else:
            # Если ссылка оказалась невалидной, удаляем её из базы
            if status in ["invalid_token", "error"]:
                o["gps_link"] = None
    
    raise HTTPException(404, "GPS недоступен")

@app.get("/api/admin/report")
async def get_report(user=Depends(check_admin)):
    in_transit = [o for o in DB.values() if str(o.get("status", "")).lower() in ['в пути', 'планируется']]
    # Теперь в отчете без GPS будут и те, у кого ссылка невалидна
    return {"total": len(in_transit), "with_gps": [o for o in in_transit if o.get("gps_link")], "without_gps": [o for o in in_transit if not o.get("gps_link")]}

# ============ ИНТЕРФЕЙС ============
@app.get("/", response_class=HTMLResponse)
async def user_portal():
    return """
<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>ТПК — Отслеживание</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<style>
    :root { --bg: #f0f2f5; --card: #fff; --text: #1a1a1a; --text-sec: #6c757d; --border: #e0e0e0; --primary: #007bff; --success: #28a745; --shadow: 0 2px 12px rgba(0,0,0,0.08); }
    [data-theme="dark"] { --bg: #1a1a1a; --card: #2d2d2d; --text: #e8e8e8; --text-sec: #a0a0a0; --border: #404040; --primary: #4a9eff; --success: #28a745; }
    * { margin: 0; padding: 0; box-sizing: border-box; }
    body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: var(--bg); color: var(--text); padding: 24px; }
    .header { max-width: 1100px; margin: 0 auto 24px; display: flex; justify-content: space-between; align-items: center; }
    .header h1 { font-size: 28px; }
    .controls { max-width: 1100px; margin: 0 auto 16px; display: flex; gap: 10px; flex-wrap: wrap; align-items: center; }
    .search-box { position: relative; flex: 1; min-width: 250px; }
    .search-box input { width: 100%; padding: 14px 20px 14px 48px; border-radius: 12px; border: 2px solid var(--border); background: var(--card); color: var(--text); }
    .search-box::before { content: '🔍'; position: absolute; left: 16px; top: 50%; transform: translateY(-50%); }
    .sort-select { padding: 14px; border-radius: 12px; border: 2px solid var(--border); background: var(--card); color: var(--text); cursor: pointer; font-weight: 600; outline: none; }
    .tabs { max-width: 1100px; margin: 0 auto 24px; display: flex; gap: 8px; background: var(--card); padding: 8px; border-radius: 12px; box-shadow: var(--shadow); }
    .tab { flex: 1; padding: 12px; border: none; background: transparent; color: var(--text-sec); font-weight: 600; cursor: pointer; border-radius: 8px; }
    .tab.active { background: var(--primary); color: white; }
    .container { max-width: 1100px; margin: 0 auto; }
    .orders-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(320px, 1fr)); gap: 16px; }
    .order-card { background: var(--card); border-radius: 12px; padding: 20px; box-shadow: var(--shadow); cursor: pointer; border-left: 4px solid var(--primary); transition: 0.2s; }
    .order-card:hover { transform: translateY(-2px); }
    .order-card.delivered { border-left-color: var(--success); }
    .order-id { color: var(--primary); font-weight: 700; font-size: 16px; }
    .order-row { font-size: 13px; margin: 6px 0; color: var(--text-sec); }
    .order-row strong { color: var(--text); min-width: 80px; display: inline-block; }
    .loc-info { margin-top: 10px; padding: 8px; background: rgba(0,123,255,0.08); border-radius: 6px; font-size: 13px; color: var(--primary); }
    .order-status { padding: 4px 10px; border-radius: 12px; font-size: 11px; font-weight: 700; }
    .in-transit { background: #d4edda; color: #155724; }
    .delivered { background: #d1ecf1; color: #0c5460; }
    #modal { display: none; position: fixed; inset: 0; background: rgba(0,0,0,0.6); z-index: 1000; padding: 20px; overflow-y: auto; }
    #modal.open { display: block; }
    .modal-content { background: var(--card); max-width: 900px; margin: 40px auto; border-radius: 16px; padding: 28px; position: relative; }
    .modal-close { position: absolute; top: 16px; right: 16px; font-size: 24px; cursor: pointer; border: none; background: none; }
    #map { height: 400px; margin-top: 20px; border-radius: 12px; display: flex; align-items: center; justify-content: center; background: #eaeaea; color: #888; }
    .modal-info { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; margin-top: 20px; }
    .modal-info strong { display: block; color: var(--text-sec); font-size: 12px; text-transform: uppercase; }
    .spinner { border: 4px solid #f3f3f3; border-top: 4px solid #3498db; border-radius: 50%; width: 30px; height: 30px; animation: spin 1s linear infinite; margin-bottom: 10px; }
    @keyframes spin { 0% { transform: rotate(0deg); } 100% { transform: rotate(360deg); } }
</style>
</head>
<body>
    <div class="header"><h1>📍 ТПК — Отслеживание</h1><button class="theme-toggle" onclick="toggleTheme()" style="background:none;border:none;font-size:24px;cursor:pointer;">🌙</button></div>
    
    <div class="controls">
        <div class="search-box"><input id="searchInput" placeholder="Поиск по номеру, городу или машине..." oninput="debounceSearch()"></div>
        <select class="sort-select" id="sortSelect" onchange="loadOrders()">
            <option value="gps">📍 Сначала с GPS</option>
            <option value="order_id">🔢 По номеру заказа</option>
            <option value="city">🏙️ По городу</option>
            <option value="date">📅 По дате прибытия</option>
            <option value="weight">⚖️ По весу</option>
        </select>
    </div>

    <div class="tabs">
        <button class="tab active" data-status="in_transit" onclick="setTab('in_transit')">🚚 В пути</button>
        <button class="tab" data-status="delivered" onclick="setTab('delivered')">✅ Выгружено</button>
        <button class="tab" data-status="with_gps" onclick="setTab('with_gps')">📍 С GPS</button>
    </div>
    <div class="container"><div class="orders-grid" id="ordersGrid"></div></div>
    
    <div id="modal"><div class="modal-content"><button class="modal-close" onclick="closeModal()">×</button><div id="modalContent"></div></div></div>
    <script>
    let currentTab = 'in_transit'; let map = null;
    function toggleTheme() { const isDark = document.documentElement.getAttribute('data-theme') === 'dark'; document.documentElement.setAttribute('data-theme', isDark ? 'light' : 'dark'); localStorage.setItem('theme', isDark ? 'light' : 'dark'); }
    document.documentElement.setAttribute('data-theme', localStorage.getItem('theme') || 'light');
    
    function setTab(s) { currentTab = s; document.querySelectorAll('.tab').forEach(t=>t.classList.remove('active')); document.querySelector(`.tab[data-status="${s}"]`).classList.add('active'); loadOrders(); }
    function debounceSearch() { clearTimeout(window.searchTimeout); window.searchTimeout = setTimeout(loadOrders, 300); }
    
    async function loadOrders() {
        const search = document.getElementById('searchInput').value.trim();
        const sort = document.getElementById('sortSelect').value;
        document.getElementById('ordersGrid').innerHTML = '<div style="text-align:center;width:100%;">Загрузка...</div>';
        
        try {
            const res = await fetch(`/api/shipments?status=${currentTab}&sort_by=${sort}${search ? '&search='+search : ''}`);
            const orders = await res.json();
            const grid = document.getElementById('ordersGrid');
            
            if (!orders.length) { grid.innerHTML = '<div style="text-align:center;width:100%;">Нет заказов</div>'; return; }
            
            grid.innerHTML = orders.map(o => {
                let badge = o.status === 'в пути' ? '<span class="order-status in-transit">В пути</span>' : (o.status === 'завершено' ? '<span class="order-status delivered">Выгружено</span>' : '');
                let loc = o.comment ? `<div class="loc-info">📝 ${o.comment}</div>` : '';
                
                return `<div class="order-card ${o.status === 'завершено' ? 'delivered' : ''}" onclick='openOrder(${JSON.stringify(o).replace(/'/g,"&#39;")})'>
                    <div style="display:flex; justify-content:space-between; margin-bottom:10px;"><span class="order-id">${o.order_id}</span>${badge}</div>
                    <div class="order-row"><strong>Адрес:</strong> ${o.address || '—'}</div>
                    <div class="order-row"><strong>Перевозчик:</strong> ${o.carrier || '—'}</div>
                    <div class="order-row"><strong>Машина:</strong> ${o.vehicle || '—'}</div>
                    <div class="order-row"><strong>Вес:</strong> ${o.weight ? o.weight + ' кг' : '—'}</div>
                    ${loc}
                </div>`;
            }).join('');
        } catch(e) {
            document.getElementById('ordersGrid').innerHTML = '<div style="text-align:center;width:100%;color:red;">Ошибка загрузки данных</div>';
        }
    }
    
    async function openOrder(o) {
        document.getElementById('modal').classList.add('open');
        document.getElementById('modalContent').innerHTML = `<h2>📦 ${o.order_id}</h2>
            <div class="modal-info">
                <div><strong>Адрес</strong>${o.address || '—'}</div>
                <div><strong>Перевозчик</strong>${o.carrier || '—'}</div>
                <div><strong>Машина</strong>${o.vehicle || '—'}</div>
                <div><strong>Водитель</strong>${o.driver || '—'}</div>
                <div><strong>Вес</strong>${o.weight ? o.weight + ' кг' : '—'}</div>
                <div><strong>Прибытие</strong>${o.plan_arrival || '—'}</div>
            </div>
            ${o.comment ? `<div style="margin-top:20px; padding:12px; background:rgba(0,0,0,0.05); border-radius:8px;"><strong>Примечание:</strong> ${o.comment}</div>` : ''}
            <div id="map"><div class="spinner"></div>Загрузка координат...</div>`;
            
        if (o.current_lat && o.current_lng) {
            renderMap(o.current_lat, o.current_lng);
        } else if (o.gps_link) {
            try {
                const res = await fetch(`/api/coords/${o.order_id}`);
                if (res.ok) {
                    const data = await res.json();
                    renderMap(data.lat, data.lng);
                } else {
                    // Если ссылка невалидна, выводим как "без GPS"
                    document.getElementById('map').innerHTML = '<div style="text-align:center;padding:50px;color:#888;">GPS отсутствует (ссылка недействительна). Ориентируйтесь на примечание.</div>';
                }
            } catch(e) {
                document.getElementById('map').innerHTML = '<div style="text-align:center;padding:50px;color:#888;">Ошибка сети.</div>';
            }
        } else {
            document.getElementById('map').innerHTML = '<div style="text-align:center;padding:50px;color:#888;">GPS отсутствует. Ориентируйтесь на примечание.</div>';
        }
    }
    
    function renderMap(lat, lng) {
        const mapDiv = document.getElementById('map');
        mapDiv.innerHTML = '';
        if (map) map.remove();
        map = L.map('map').setView([lat, lng], 8);
        L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png').addTo(map);
        L.marker([lat, lng]).addTo(map);
    }

    function closeModal() { document.getElementById('modal').classList.remove('open'); if(map){map.remove(); map=null;} }
    loadOrders();
    </script>
</body>
</html>
"""

# ============ АДМИН-ПАНЕЛЬ (ОТЧЕТ) ============
@app.get("/admin", response_class=HTMLResponse)
async def admin_panel(user=Depends(check_admin)):
    return """
    <!DOCTYPE html>
    <html>
    <head>
        <title>Админ-панель: Отчет по транспорту</title>
        <style>
            body { font-family: Arial; padding: 20px; background: #f0f2f5; }
            h2 { margin-top: 30px; color: #333; }
            table { border-collapse: collapse; width: 100%; background: white; font-size: 13px; margin-bottom: 20px; box-shadow: 0 2px 8px rgba(0,0,0,0.1); }
            th, td { border: 1px solid #ddd; padding: 10px; text-align: left; }
            th { background: #007bff; color: white; position: sticky; top: 0; }
            tr:hover { background: #f0f8ff; }
            .no-gps { background: #fff3cd; }
            .gps-yes { color: green; font-weight: bold; }
        </style>
    </head>
    <body>
        <h1>📊 Отчет: Транспорт в пути</h1>
        <p>Разделение машин по наличию GPS-трекинга.</p>
        
        <h2>🟢 В пути с GPS (Базовый трекинг)</h2>
        <table id="table-gps">
            <thead><tr><th>Заказ</th><th>Машина</th><th>Адрес</th><th>GPS Ссылка</th><th>Примечание</th></tr></thead>
            <tbody></tbody>
        </table>

        <h2>🟡 В пути без GPS (Только по примечанию)</h2>
        <table id="table-no-gps">
            <thead><tr><th>Заказ</th><th>Машина</th><th>Адрес</th><th>Примечание (Местоположение)</th></tr></thead>
            <tbody></tbody>
        </table>

        <script>
        async function loadReport() {
            const res = await fetch('/api/admin/report');
            const data = await res.json();
            
            document.querySelector('#table-gps tbody').innerHTML = data.with_gps.map(o => `
                <tr>
                    <td><b>${o.order_id}</b></td>
                    <td>${o.vehicle || '—'}</td>
                    <td>${o.address || '—'}</td>
                    <td><a href="${o.gps_link}" target="_blank" class="gps-yes">📍 Открыть</a></td>
                    <td>${o.comment || '—'}</td>
                </tr>
            `).join('');
            
            document.querySelector('#table-no-gps tbody').innerHTML = data.without_gps.map(o => `
                <tr class="no-gps">
                    <td><b>${o.order_id}</b></td>
                    <td>${o.vehicle || '—'}</td>
                    <td>${o.address || '—'}</td>
                    <td><b>${o.comment || 'Нет данных о местоположении'}</b></td>
                </tr>
            `).join('');
        }
        loadReport();
        </script>
    </body>
    </html>
    """
