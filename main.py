from fastapi import FastAPI, HTTPException, Depends
from fastapi.responses import HTMLResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel
from typing import Optional
import os, json, httpx
from datetime import date, datetime, timedelta

app = FastAPI(title="ТПК Отслеживание")
security = HTTPBasic()

# Настройки админа
ADMIN_USER = os.environ.get("ADMIN_USER", "admin")
ADMIN_PASS = os.environ.get("ADMIN_PASS", "admin123")
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN")

# Глобальная "база данных" в памяти
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

class TabSettings(BaseModel):
    show_in_transit: bool = True
    show_delivered: bool = True
    show_no_status: bool = True
    show_with_gps: bool = True

@app.on_event("startup")
async def startup():
    global DB
    # Загружаем данные из JSON файла
    if os.path.exists("shipments.json"):
        with open("shipments.json", "r", encoding="utf-8") as f:
            DB = json.load(f)
        print(f"Успешно загружено {len(DB)} заказов из shipments.json")
    else:
        print("ВНИМАНИЕ: файл shipments.json не найден!")

def check_admin(credentials: HTTPBasicCredentials = Depends(security)):
    if credentials.username != ADMIN_USER or credentials.password != ADMIN_PASS:
        raise HTTPException(401, "Неверный логин/пароль")
    return credentials.username

@app.get("/api/settings/tabs")
async def get_tab_settings():
    return SETTINGS

@app.post("/api/admin/settings/tabs")
async def set_tab_settings(settings: TabSettings, user=Depends(check_admin)):
    global SETTINGS
    SETTINGS = settings.model_dump()
    return {"ok": True}

@app.get("/api/shipments")
async def list_shipments(status: Optional[str] = None, search: Optional[str] = None):
    result = []
    for o in DB.values():
        # Фильтрация по статусу
        s = o.get("status", "")
        if status == "in_transit" and s not in ['в пути', 'планируется']: continue
        if status == "delivered" and s not in ['завершено', 'выгружено']: continue
        if status == "no_status" and s != '': continue
        if status == "with_gps" and not o.get("gps_link"): continue
        
        # Поиск
        if search:
            q = search.lower()
            if q not in o.get("order_id", "").lower() and q not in o.get("address", "").lower() and q not in o.get("vehicle", "").lower():
                continue
        result.append(o)
    
    # Сортировка по дате прибытия
    result.sort(key=lambda x: x.get("plan_arrival") or "9999-12-31")
    return result

@app.get("/api/track/{order_id}")
async def track_order(order_id: str):
    if order_id not in DB:
        raise HTTPException(404, f"Заказ {order_id} не найден")
    return DB[order_id]

@app.post("/api/admin/refresh/all")
async def admin_refresh_all(user=Depends(check_admin)):
    # Заглушка для парсера, так как мы теперь работаем без БД
    return {"ok": True, "message": "Парсер в разработке"}

@app.get("/api/admin/shipments")
async def admin_list(user=Depends(check_admin)):
    return list(DB.values())

@app.post("/api/admin/shipments")
async def admin_create(s: Shipment, user=Depends(check_admin)):
    DB[s.order_id] = s.model_dump()
    return {"ok": True}

@app.delete("/api/admin/shipments/{order_id}")
async def admin_delete(order_id: str, user=Depends(check_admin)):
    if order_id in DB:
        del DB[order_id]
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
    :root { --bg: #f0f2f5; --card: #fff; --text: #1a1a1a; --text-sec: #6c757d; --border: #e0e0e0; --primary: #007bff; --success: #28a745; --warn: #ffc107; --danger: #dc3545; --shadow: 0 2px 12px rgba(0,0,0,0.08); --shadow-hover: 0 4px 20px rgba(0,0,0,0.12); }
    [data-theme="dark"] { --bg: #1a1a1a; --card: #2d2d2d; --text: #e8e8e8; --text-sec: #a0a0a0; --border: #404040; --primary: #4a9eff; --success: #28a745; --warn: #ffc107; --danger: #dc3545; --shadow: 0 2px 12px rgba(0,0,0,0.3); --shadow-hover: 0 4px 20px rgba(0,0,0,0.4); }
    * { margin: 0; padding: 0; box-sizing: border-box; }
    body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: var(--bg); color: var(--text); padding: 24px; transition: background 0.3s, color 0.3s; }
    .header { max-width: 1100px; margin: 0 auto 24px; display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 12px; }
    .header h1 { font-size: 28px; display: flex; align-items: center; gap: 8px; }
    .theme-toggle { background: var(--card); border: 1px solid var(--border); border-radius: 8px; padding: 8px 16px; cursor: pointer; font-size: 18px; transition: all 0.2s; color: var(--text); }
    .theme-toggle:hover { box-shadow: var(--shadow); transform: translateY(-1px); }
    
    .search-box { position: relative; max-width: 1100px; margin: 0 auto 16px; }
    .search-box input { width: 100%; padding: 14px 20px 14px 48px; font-size: 16px; border: 2px solid var(--border); border-radius: 12px; background: var(--card); color: var(--text); transition: all 0.2s; outline: none; }
    .search-box input:focus { border-color: var(--primary); box-shadow: 0 0 0 3px rgba(0,123,255,0.15); }
    .search-box::before { content: '🔍'; position: absolute; left: 16px; top: 50%; transform: translateY(-50%); font-size: 18px; pointer-events: none; }
    .search-hint { max-width: 1100px; margin: 0 auto 16px; font-size: 13px; color: var(--text-sec); }
    
    .tabs { max-width: 1100px; margin: 0 auto 24px; display: flex; gap: 8px; flex-wrap: wrap; background: var(--card); padding: 8px; border-radius: 12px; box-shadow: var(--shadow); }
    .tab { flex: 1; min-width: 140px; padding: 12px 20px; border: none; background: transparent; color: var(--text-sec); font-size: 14px; font-weight: 600; cursor: pointer; border-radius: 8px; transition: all 0.2s; display: flex; align-items: center; justify-content: center; gap: 6px; }
    .tab:hover { background: rgba(0,123,255,0.08); color: var(--primary); }
    .tab.active { background: var(--primary); color: white; box-shadow: 0 2px 8px rgba(0,123,255,0.3); }
    .tab.hidden { display: none; }
    .count { background: rgba(0,0,0,0.1); padding: 2px 8px; border-radius: 10px; font-size: 12px; }
    .tab.active .count { background: rgba(255,255,255,0.25); }
    .container { max-width: 1100px; margin: 0 auto; }
    .orders-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(320px, 1fr)); gap: 16px; }
    .order-card { background: var(--card); border-radius: 12px; padding: 20px; box-shadow: var(--shadow); transition: all 0.3s; cursor: pointer; border-left: 4px solid var(--primary); animation: fadeIn 0.3s ease; }
    .order-card:hover { box-shadow: var(--shadow-hover); transform: translateY(-2px); }
    .order-card.delivered { border-left-color: var(--success); }
    .order-card.no-status { border-left-color: var(--warn); }
    .order-card h3 { font-size: 16px; margin-bottom: 12px; display: flex; justify-content: space-between; align-items: center; }
    .order-id { color: var(--primary); font-weight: 700; }
    .order-row { font-size: 13px; margin: 6px 0; display: flex; gap: 8px; color: var(--text-sec); }
    .order-row strong { color: var(--text); min-width: 80px; }
    .order-status { display: inline-block; padding: 4px 10px; border-radius: 12px; font-size: 11px; font-weight: 700; text-transform: uppercase; }
    .order-status.in-transit { background: #d4edda; color: #155724; }
    .order-status.delivered { background: #d1ecf1; color: #0c5460; }
    .order-status.no-status { background: #fff3cd; color: #856404; }
    .empty-state { text-align: center; padding: 80px 20px; color: var(--text-sec); }
    .empty-state-icon { font-size: 64px; margin-bottom: 16px; opacity: 0.6; }
    .skeleton { background: linear-gradient(90deg, var(--border) 25%, rgba(255,255,255,0.1) 50%, var(--border) 75%); background-size: 200% 100%; animation: shimmer 1.5s infinite; border-radius: 8px; height: 140px; }
    @keyframes shimmer { 0% { background-position: -200% 0; } 100% { background-position: 200% 0; } }
    @keyframes fadeIn { from { opacity: 0; transform: translateY(8px); } to { opacity: 1; transform: translateY(0); } }
    #modal { display: none; position: fixed; inset: 0; background: rgba(0,0,0,0.6); z-index: 1000; padding: 20px; overflow-y: auto; }
    #modal.open { display: block; }
    .modal-content { background: var(--card); max-width: 900px; margin: 40px auto; border-radius: 16px; padding: 28px; position: relative; animation: fadeIn 0.3s; }
    .modal-close { position: absolute; top: 16px; right: 16px; background: transparent; border: none; font-size: 24px; cursor: pointer; color: var(--text-sec); padding: 8px; border-radius: 8px; }
    .modal-close:hover { background: var(--bg); }
    #map { height: 400px; margin-top: 20px; border-radius: 12px; }
    .modal-info { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 12px; margin-top: 20px; }
    .modal-info > div { font-size: 14px; }
    .modal-info strong { display: block; color: var(--text-sec); font-size: 12px; margin-bottom: 4px; text-transform: uppercase; letter-spacing: 0.5px; }
    .modal-info span { color: var(--text); font-size: 15px; }
    mark { background: #ffe066; color: #000; padding: 0 2px; border-radius: 3px; }
    @media (max-width: 640px) { body { padding: 16px; } .tab { min-width: 100%; } .orders-grid { grid-template-columns: 1fr; } }
</style>
</head>
<body>
    <div class="header">
        <h1>📍 ТПК — Отслеживание</h1>
        <button class="theme-toggle" onclick="toggleTheme()"><span id="themeIcon">🌙</span></button>
    </div>

    <div class="search-box">
        <input id="searchInput" placeholder="Поиск по номеру заказа (мин. 4 цифры)..." oninput="debounceSearch()" autocomplete="off">
    </div>
    <div class="search-hint" id="searchHint">💡 Введите, например, <b>1587</b>, чтобы найти ТПК00001587</div>

    <div class="tabs" id="tabsContainer">
        <button class="tab active" data-status="in_transit" onclick="setTab('in_transit')">🚚 В пути <span class="count" id="count-in_transit">0</span></button>
        <button class="tab" data-status="delivered" onclick="setTab('delivered')">✅ Выгружено <span class="count" id="count-delivered">0</span></button>
        <button class="tab" data-status="no_status" onclick="setTab('no_status')">⚠️ Без статуса <span class="count" id="count-no_status">0</span></button>
        <button class="tab" data-status="with_gps" onclick="setTab('with_gps')">📍 Все с GPS <span class="count" id="count-with_gps">0</span></button>
    </div>

    <div class="container"><div class="orders-grid" id="ordersGrid"></div></div>

    <div id="modal"><div class="modal-content"><button class="modal-close" onclick="closeModal()">×</button><div id="modalContent"></div></div></div>

    <script>
    let currentTab = 'in_transit'; let map = null; let searchTimeout = null; let tabSettings = {};

    function toggleTheme() {
        const html = document.documentElement; const isDark = html.getAttribute('data-theme') === 'dark';
        html.setAttribute('data-theme', isDark ? 'light' : 'dark'); document.getElementById('themeIcon').textContent = isDark ? '🌙' : '☀️'; localStorage.setItem('theme', isDark ? 'light' : 'dark');
    }
    const savedTheme = localStorage.getItem('theme') || 'light'; document.documentElement.setAttribute('data-theme', savedTheme); document.getElementById('themeIcon').textContent = savedTheme === 'dark' ? '☀️' : '🌙';

    function debounceSearch() {
        clearTimeout(searchTimeout);
        searchTimeout = setTimeout(() => loadOrders(), 300);
    }

    async function updateTabsVisibility() {
        try {
            const res = await fetch('/api/settings/tabs');
            tabSettings = await res.json();
            document.querySelectorAll('.tab').forEach(tab => {
                const key = tab.dataset.status;
                const settingKey = 'show_' + key;
                if (tabSettings[settingKey] === false) {
                    tab.classList.add('hidden');
                } else {
                    tab.classList.remove('hidden');
                }
            });
        } catch(e) { console.error("Ошибка загрузки настроек вкладок:", e); }
    }

    function setTab(status) {
        currentTab = status; 
        document.querySelectorAll('.tab').forEach(t => t.classList.remove('active')); 
        document.querySelector(`.tab[data-status="${status}"]`)?.classList.add('active'); 
        loadOrders();
    }

    async function loadOrders() {
        const grid = document.getElementById('ordersGrid');
        const search = document.getElementById('searchInput').value.trim();
        grid.innerHTML = '<div class="skeleton"></div><div class="skeleton"></div><div class="skeleton"></div>';
        
        try {
            const url = `/api/shipments?status=${currentTab}${search ? '&search=' + encodeURIComponent(search) : ''}`;
            const res = await fetch(url); 
            if (!res.ok) throw new Error(`HTTP ${res.status}`);
            const orders = await res.json();
            
            if (search) {
                document.getElementById('searchHint').innerHTML = `🔍 Найдено по запросу «${search}»: <b>${orders.length}</b>`;
            } else {
                document.getElementById('searchHint').innerHTML = `💡 Введите, например, <b>1587</b>, чтобы найти ТПК00001587`;
            }
            
            if (orders.length === 0) {
                grid.innerHTML = `<div class="empty-state" style="grid-column: 1 / -1;"><div class="empty-state-icon">${search ? '🔍' : '📭'}</div><h2>${search ? 'Ничего не найдено' : 'Нет заказов'}</h2><p>${search ? 'Попробуйте другой запрос' : getEmptyMsg(currentTab)}</p></div>`;
            } else {
                grid.innerHTML = orders.map(o => renderCard(o, search)).join('');
            }
        } catch (e) {
            console.error("Ошибка загрузки списка заказов:", e);
            grid.innerHTML = `<div class="empty-state" style="grid-column: 1 / -1;"><div class="empty-state-icon">❌</div><h2>Ошибка загрузки</h2><p>${e.message}</p></div>`;
        }

        try {
            const [allRes, delRes, noStatusRes, gpsRes] = await Promise.all([
                fetch('/api/shipments?status=in_transit'),
                fetch('/api/shipments?status=delivered'),
                fetch('/api/shipments?status=no_status'),
                fetch('/api/shipments?status=with_gps')
            ]);
            const allCounts = await allRes.json();
            const delCounts = await delRes.json();
            const noStatusCounts = await noStatusRes.json();
            const gpsCounts = await gpsRes.json();
            
            document.getElementById('count-in_transit').textContent = allCounts.length;
            document.getElementById('count-delivered').textContent = delCounts.length;
            document.getElementById('count-no_status').textContent = noStatusCounts.length;
            document.getElementById('count-with_gps').textContent = gpsCounts.length;
        } catch (e) { console.error("Ошибка обновления счётчиков:", e); }
    }

    function highlight(text, query) {
        if (!query || !text) return text || '';
        const safeQuery = query.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
        const regex = new RegExp('(' + safeQuery + ')', 'gi');
        return text.replace(regex, '<mark>$1</mark>');
    }

    function getEmptyMsg(tab) {
        if (tab === 'in_transit') return 'Все машины уже прибыли или ожидают выезда';
        if (tab === 'delivered') return 'Пока нет завершённых перевозок';
        if (tab === 'no_status') return 'Все заказы имеют статус';
        return 'Нет заказов с GPS-ссылками';
    }

    function renderCard(o, query) {
        let statusBadge = '';
        if (o.status === 'в пути' || o.status === 'планируется') statusBadge = '<span class="order-status in-transit">В пути</span>';
        else if (o.status === 'завершено' || o.status === 'выгружено') statusBadge = '<span class="order-status delivered">Выгружено</span>';
        else statusBadge = '<span class="order-status no-status">Без статуса</span>';
        
        const comment = o.comment ? `<div class="order-row"><strong>Примечание:</strong> ${o.comment.substring(0, 80)}${o.comment.length > 80 ? '...' : ''}</div>` : '';
        const arrival = o.plan_arrival ? `<div class="order-row"><strong>Прибытие:</strong> ${o.plan_arrival}</div>` : '';
        const highlightedId = highlight(o.order_id, query);
        
        return `<div class="order-card ${o.status === 'завершено' || o.status === 'выгружено' ? 'delivered' : (!o.status ? 'no-status' : '')}" onclick='openOrder(${JSON.stringify(o).replace(/'/g,"&#39;")})'>
            <h3><span class="order-id">${highlightedId}</span>${statusBadge}</h3>
            <div class="order-row"><strong>Адрес:</strong> ${o.address || '—'}</div>
            <div class="order-row"><strong>Перевозчик:</strong> ${o.carrier || '—'}</div>
            <div class="order-row"><strong>Машина:</strong> ${o.vehicle || '—'}</div>
            ${arrival} ${comment}
        </div>`;
    }

    async function openOrder(o) {
        document.getElementById('modal').classList.add('open');
        const content = document.getElementById('modalContent');
        content.innerHTML = `<h2 style="margin-bottom:16px;">📦 ${o.order_id}</h2><div class="modal-info">
            <div><strong>Адрес доставки</strong><span>${o.address || '—'}</span></div>
            <div><strong>Перевозчик</strong><span>${o.carrier || '—'}</span></div>
            <div><strong>Машина</strong><span>${o.vehicle || '—'}</span></div>
            <div><strong>Водитель</strong><span>${o.driver || '—'}</span></div>
            <div><strong>Статус</strong><span>${o.status || 'Без статуса'}</span></div>
            <div><strong>План. прибытие</strong><span>${o.plan_arrival || '—'}</span></div>
            ${o.comment ? `<div style="grid-column: 1 / -1;"><strong>Примечание</strong><span>${o.comment}</span></div>` : ''}
        </div><div id="map"></div>`;
        if (o.current_lat && o.current_lng) {
            if (map) map.remove();
            map = L.map('map').setView([o.current_lat, o.current_lng], 8);
            L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png').addTo(map);
            L.marker([o.current_lat, o.current_lng]).addTo(map).bindPopup(`<b>${o.order_id}</b>`).openPopup();
        } else {
            content.querySelector('#map').innerHTML = '<div style="text-align:center;padding:80px;color:#888;">⏳ Ожидаем координаты...</div>';
        }
    }
    function closeModal() { document.getElementById('modal').classList.remove('open'); if (map) { map.remove(); map = null; } }
    document.getElementById('modal').addEventListener('click', e => { if (e.target.id === 'modal') closeModal(); });
    
    loadOrders();
    updateTabsVisibility();
    </script>
</body>
</html>
"""

# ============ АДМИН-ПАНЕЛЬ ============
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
            button { padding:6px 12px; margin:2px; cursor:pointer; }
            .btn-add { background:#28a745; color:white; border:none; border-radius:4px; font-size:14px; padding:10px 20px; margin-bottom:10px; }
            #filter { padding:8px; width:300px; margin-bottom:10px; }
            #modal { display:none; position:fixed; top:0; left:0; width:100%; height:100%; background:rgba(0,0,0,0.5); }
            #modalContent { background:white; max-width:600px; margin:80px auto; padding:30px; border-radius:8px; max-height:80vh; overflow-y:auto; }
            #modalContent label { display:block; margin-top:10px; font-weight:bold; }
            #modalContent input, #modalContent select { width:100%; padding:8px; box-sizing:border-box; }
            .modal-buttons { margin-top:20px; text-align:right; }
        </style>
    </head>
    <body>
        <h1>🚚 Админ-панель — Отгрузки</h1>
        <button class="btn-add" onclick="openAdd()">➕ Добавить</button>
        <input id="filter" placeholder="Поиск..." oninput="renderTable()"/>
        <table id="shipments">
            <thead><tr><th>Заказ</th><th>Адрес</th><th>Перевозчик</th><th>Машина</th><th>Статус</th><th>Действия</th></tr></thead>
            <tbody></tbody>
        </table>

        <div id="modal">
            <div id="modalContent">
                <h2 id="modalTitle">Добавить заказ</h2>
                <label>Номер заказа *</label><input id="f_order_id"/>
                <label>Адрес доставки</label><input id="f_address"/>
                <label>Перевозчик</label><input id="f_carrier"/>
                <label>Машина</label><input id="f_vehicle"/>
                <label>Статус</label>
                <select id="f_status"><option value="">Без статуса</option><option>планируется</option><option>в пути</option><option>выгружено</option><option>завершено</option></select>
                <div class="modal-buttons"><button onclick="closeModal()">Отмена</button><button class="btn-add" onclick="saveOrder()">Сохранить</button></div>
            </div>
        </div>

        <script>
        let allData=[];
        async function load(){
            const res = await fetch('/api/admin/shipments'); allData = await res.json();
            renderTable();
        }
        function renderTable(){
            const q = document.getElementById('filter').value.toLowerCase(); 
            let filtered = allData.filter(s => (s.order_id||'').toLowerCase().includes(q) || (s.address||'').toLowerCase().includes(q));
            document.querySelector('#shipments tbody').innerHTML = filtered.map(s=>`
                <tr><td>${s.order_id}</td><td>${s.address||''}</td><td>${s.carrier||''}</td><td>${s.vehicle||''}</td>
                <td>${s.status||'—'}</td>
                <td><button onclick='openEdit(${JSON.stringify(s).replace(/'/g,"&#39;")})'>✏️</button><button onclick="del('${s.order_id}')">🗑</button></td></tr>
            `).join('');
        }
        function openAdd(){ ['order_id','address','carrier','vehicle'].forEach(f=>document.getElementById('f_'+f).value=''); document.getElementById('f_status').value=''; document.getElementById('modalTitle').innerText='Добавить заказ'; document.getElementById('modal').style.display='block'; }
        function openEdit(s){ ['order_id','address','carrier','vehicle'].forEach(f=>document.getElementById('f_'+f).value=s[f]||''); document.getElementById('f_status').value=s.status||''; document.getElementById('modalTitle').innerText='Редактировать: '+s.order_id; document.getElementById('modal').style.display='block'; }
        function closeModal(){ document.getElementById('modal').style.display='none'; }
        async function saveOrder(){
            const payload = { order_id: document.getElementById('f_order_id').value, address: document.getElementById('f_address').value, carrier: document.getElementById('f_carrier').value, vehicle: document.getElementById('f_vehicle').value, status: document.getElementById('f_status').value };
            await fetch('/api/admin/shipments',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
            closeModal(); load();
        }
        async function del(order_id){ if(!confirm('Удалить '+order_id+'?')) return; await fetch('/api/admin/shipments/'+order_id,{method:'DELETE'}); load(); }
        load();
        </script>
    </body>
    </html>
    """
