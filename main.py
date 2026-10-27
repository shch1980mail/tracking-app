from fastapi import FastAPI, HTTPException, Depends
from fastapi.responses import HTMLResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel
from typing import Optional
import os, json

app = FastAPI(title="ТПК Отслеживание")
security = HTTPBasic()

ADMIN_USER = os.environ.get("ADMIN_USER", "admin")
ADMIN_PASS = os.environ.get("ADMIN_PASS", "admin123")

DB = {}
SETTINGS = {"show_in_transit": True, "show_delivered": True, "show_no_status": True, "show_with_gps": True}

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

@app.on_event("startup")
async def startup():
    global DB
    if os.path.exists("shipments.json"):
        with open("shipments.json", "r", encoding="utf-8") as f:
            DB = json.load(f)
        print(f"✅ Загружено {len(DB)} заказов")

def check_admin(credentials: HTTPBasicCredentials = Depends(security)):
    if credentials.username != ADMIN_USER or credentials.password != ADMIN_PASS:
        raise HTTPException(401, "Неверный логин/пароль")
    return credentials.username

@app.get("/api/shipments")
async def list_shipments(status: Optional[str] = None, search: Optional[str] = None):
    result = []
    for o in DB.values():
        s = str(o.get("status", "")).lower()
        
        # Строгая фильтрация для вкладок
        if status == "in_transit" and s not in ['в пути', 'планируется']: continue
        if status == "delivered" and s not in ['завершено', 'выгружено']: continue
        if status == "no_status" and s != '': continue
        
        # Если ищем "с GPS", проверяем наличие ссылки
        if status == "with_gps" and not o.get("gps_link"): continue
        
        if search:
            q = search.lower()
            if q not in o.get("order_id", "").lower() and q not in o.get("address", "").lower() and q not in o.get("vehicle", "").lower():
                continue
        result.append(o)
    
    # Сортировка: сначала те, у кого есть GPS, потом по дате
    result.sort(key=lambda x: (not bool(x.get("gps_link")), x.get("plan_arrival") or "9999-12-31"))
    return result

# === ОТЧЕТ ДЛЯ АДМИНКИ ===
@app.get("/api/admin/report")
async def get_report(user=Depends(check_admin)):
    in_transit = [o for o in DB.values() if str(o.get("status", "")).lower() in ['в пути', 'планируется']]
    
    with_gps = [o for o in in_transit if o.get("gps_link")]
    without_gps = [o for o in in_transit if not o.get("gps_link")]
    
    return {
        "total": len(in_transit),
        "with_gps": with_gps,
        "without_gps": without_gps
    }

@app.get("/api/track/{order_id}")
async def track_order(order_id: str):
    if order_id not in DB:
        raise HTTPException(404, f"Заказ {order_id} не найден")
    return DB[order_id]

@app.get("/api/admin/shipments")
async def admin_list(user=Depends(check_admin)):
    return list(DB.values())

@app.post("/api/admin/shipments")
async def admin_create(s: Shipment, user=Depends(check_admin)):
    DB[s.order_id] = s.model_dump()
    return {"ok": True}

@app.delete("/api/admin/shipments/{order_id}")
async def admin_delete(order_id: str, user=Depends(check_admin)):
    if order_id in DB: del DB[order_id]
    return {"ok": True}

# ============ ПОЛЬЗОВАТЕЛЬСКИЙ ИНТЕРФЕЙС ============
@app.get("/", response_class=HTMLResponse)
async def user_portal():
    return """
<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>ТПК — Отслеживание заказов</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<style>
    :root { --bg: #f0f2f5; --card: #fff; --text: #1a1a1a; --text-sec: #6c757d; --border: #e0e0e0; --primary: #007bff; --success: #28a745; --warn: #ffc107; --shadow: 0 2px 12px rgba(0,0,0,0.08); }
    [data-theme="dark"] { --bg: #1a1a1a; --card: #2d2d2d; --text: #e8e8e8; --text-sec: #a0a0a0; --border: #404040; --primary: #4a9eff; --success: #28a745; --warn: #ffc107; }
    * { margin: 0; padding: 0; box-sizing: border-box; }
    body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: var(--bg); color: var(--text); padding: 24px; transition: background 0.3s, color 0.3s; }
    .header { max-width: 1100px; margin: 0 auto 24px; display: flex; justify-content: space-between; align-items: center; }
    .header h1 { font-size: 28px; display: flex; align-items: center; gap: 8px; }
    .theme-toggle { background: var(--card); border: 1px solid var(--border); border-radius: 8px; padding: 8px 16px; cursor: pointer; font-size: 18px; color: var(--text); }
    .search-box { position: relative; max-width: 1100px; margin: 0 auto 16px; }
    .search-box input { width: 100%; padding: 14px 20px 14px 48px; font-size: 16px; border: 2px solid var(--border); border-radius: 12px; background: var(--card); color: var(--text); outline: none; }
    .search-box::before { content: '🔍'; position: absolute; left: 16px; top: 50%; transform: translateY(-50%); font-size: 18px; }
    .tabs { max-width: 1100px; margin: 0 auto 24px; display: flex; gap: 8px; background: var(--card); padding: 8px; border-radius: 12px; box-shadow: var(--shadow); }
    .tab { flex: 1; padding: 12px; border: none; background: transparent; color: var(--text-sec); font-size: 14px; font-weight: 600; cursor: pointer; border-radius: 8px; }
    .tab.active { background: var(--primary); color: white; }
    .container { max-width: 1100px; margin: 0 auto; }
    .orders-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(320px, 1fr)); gap: 16px; }
    .order-card { background: var(--card); border-radius: 12px; padding: 20px; box-shadow: var(--shadow); cursor: pointer; border-left: 4px solid var(--primary); transition: 0.2s; }
    .order-card:hover { transform: translateY(-2px); }
    .order-card.delivered { border-left-color: var(--success); }
    .order-card.no-status { border-left-color: var(--warn); }
    .order-id { color: var(--primary); font-weight: 700; font-size: 16px; }
    .order-row { font-size: 13px; margin: 6px 0; display: flex; gap: 8px; color: var(--text-sec); }
    .order-row strong { color: var(--text); min-width: 80px; }
    .loc-info { margin-top: 10px; padding: 8px; background: rgba(0,123,255,0.08); border-radius: 6px; font-size: 13px; color: var(--primary); font-weight: 500; }
    .order-status { padding: 4px 10px; border-radius: 12px; font-size: 11px; font-weight: 700; text-transform: uppercase; }
    .in-transit { background: #d4edda; color: #155724; }
    .delivered { background: #d1ecf1; color: #0c5460; }
    #modal { display: none; position: fixed; inset: 0; background: rgba(0,0,0,0.6); z-index: 1000; padding: 20px; overflow-y: auto; }
    #modal.open { display: block; }
    .modal-content { background: var(--card); max-width: 900px; margin: 40px auto; border-radius: 16px; padding: 28px; position: relative; }
    .modal-close { position: absolute; top: 16px; right: 16px; font-size: 24px; cursor: pointer; border: none; background: none; }
    #map { height: 400px; margin-top: 20px; border-radius: 12px; }
    .modal-info { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 12px; margin-top: 20px; }
    .modal-info strong { display: block; color: var(--text-sec); font-size: 12px; text-transform: uppercase; }
</style>
</head>
<body>
    <div class="header">
        <h1>📍 ТПК — Отслеживание</h1>
        <button class="theme-toggle" onclick="toggleTheme()"><span id="themeIcon">🌙</span></button>
    </div>
    <div class="search-box"><input id="searchInput" placeholder="Поиск по номеру заказа..." oninput="debounceSearch()"></div>
    <div class="tabs">
        <button class="tab active" data-status="in_transit" onclick="setTab('in_transit')">🚚 В пути</button>
        <button class="tab" data-status="delivered" onclick="setTab('delivered')">✅ Выгружено</button>
        <button class="tab" data-status="with_gps" onclick="setTab('with_gps')">📍 С GPS</button>
    </div>
    <div class="container"><div class="orders-grid" id="ordersGrid"></div></div>
    
    <div id="modal"><div class="modal-content"><button class="modal-close" onclick="closeModal()">×</button><div id="modalContent"></div></div></div>
    <script>
    let currentTab = 'in_transit'; let map = null;
    function toggleTheme() {
        const html = document.documentElement; const isDark = html.getAttribute('data-theme') === 'dark';
        html.setAttribute('data-theme', isDark ? 'light' : 'dark'); localStorage.setItem('theme', isDark ? 'light' : 'dark');
    }
    document.documentElement.setAttribute('data-theme', localStorage.getItem('theme') || 'light');
    
    function setTab(s) { currentTab = s; document.querySelectorAll('.tab').forEach(t=>t.classList.remove('active')); document.querySelector(`.tab[data-status="${s}"]`).classList.add('active'); loadOrders(); }
    function debounceSearch() { clearTimeout(window.searchTimeout); window.searchTimeout = setTimeout(loadOrders, 300); }
    
    async function loadOrders() {
        const search = document.getElementById('searchInput').value.trim();
        const res = await fetch(`/api/shipments?status=${currentTab}${search ? '&search='+search : ''}`);
        const orders = await res.json();
        const grid = document.getElementById('ordersGrid');
        
        if (!orders.length) { grid.innerHTML = '<div style="text-align:center;width:100%;">Нет заказов</div>'; return; }
        
        grid.innerHTML = orders.map(o => {
            let badge = o.status === 'в пути' ? '<span class="order-status in-transit">В пути</span>' : (o.status === 'завершено' ? '<span class="order-status delivered">Выгружено</span>' : '');
            // ПРИОРИТЕТ: Если есть примечание (comment), выводим его как местоположение
            let locationHtml = o.comment ? `<div class="loc-info">📝 ${o.comment}</div>` : '';
            
            return `<div class="order-card ${o.status === 'завершено' ? 'delivered' : ''}" onclick='openOrder(${JSON.stringify(o).replace(/'/g,"&#39;")})'>
                <div style="display:flex; justify-content:space-between; margin-bottom:10px;">
                    <span class="order-id">${o.order_id}</span>${badge}
                </div>
                <div class="order-row"><strong>Адрес:</strong> ${o.address || '—'}</div>
                <div class="order-row"><strong>Перевозчик:</strong> ${o.carrier || '—'}</div>
                <div class="order-row"><strong>Машина:</strong> ${o.vehicle || '—'}</div>
                ${locationHtml}
            </div>`;
        }).join('');
    }
    
    function openOrder(o) {
        document.getElementById('modal').classList.add('open');
        document.getElementById('modalContent').innerHTML = `<h2>📦 ${o.order_id}</h2>
            <div class="modal-info">
                <div><strong>Адрес</strong>${o.address || '—'}</div>
                <div><strong>Перевозчик</strong>${o.carrier || '—'}</div>
                <div><strong>Машина</strong>${o.vehicle || '—'}</div>
                <div><strong>Водитель</strong>${o.driver || '—'}</div>
                <div><strong>Статус</strong>${o.status || '—'}</div>
                <div><strong>Прибытие</strong>${o.plan_arrival || '—'}</div>
            </div>
            ${o.comment ? `<div style="margin-top:20px; padding:12px; background:rgba(0,0,0,0.05); border-radius:8px;"><strong>Примечание:</strong> ${o.comment}</div>` : ''}
            <div id="map"></div>`;
            
        if (o.current_lat && o.current_lng) {
            if (map) map.remove();
            map = L.map('map').setView([o.current_lat, o.current_lng], 8);
            L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png').addTo(map);
            L.marker([o.current_lat, o.current_lng]).addTo(map);
        } else if (o.gps_link) {
            document.getElementById('map').innerHTML = '<div style="text-align:center;padding:50px;color:#888;">Ссылка на GPS есть, но координаты еще не получены</div>';
        } else {
            document.getElementById('map').innerHTML = '<div style="text-align:center;padding:50px;color:#888;">GPS отсутствует. Ориентируйтесь на примечание.</div>';
        }
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
            .no-gps { background: #fff3cd; } /* Подсветка машин без GPS */
            .gps-yes { color: green; font-weight: bold; }
        </style>
    </head>
    <body>
        <h1>📊 Отчет: Транспорт в пути</h1>
        <p>Разделение машин по наличию GPS-трекинга. Для машин без GPS используйте колонку «Примечание» для понимания их реального местоположения.</p>
        
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
            
            // Заполняем таблицу с GPS
            document.querySelector('#table-gps tbody').innerHTML = data.with_gps.map(o => `
                <tr>
                    <td><b>${o.order_id}</b></td>
                    <td>${o.vehicle || '—'}</td>
                    <td>${o.address || '—'}</td>
                    <td><a href="${o.gps_link}" target="_blank" class="gps-yes">📍 Открыть</a></td>
                    <td>${o.comment || '—'}</td>
                </tr>
            `).join('');
            
            // Заполняем таблицу без GPS
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
