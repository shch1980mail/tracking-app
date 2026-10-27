from fastapi import FastAPI, HTTPException, Depends, UploadFile, File
from fastapi.responses import HTMLResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel
from typing import Optional
import os, json, requests, asyncio, io
import pandas as pd
import openpyxl

# === ИМПОРТЫ ДЛЯ БОТА MAX (БЕЗ MessageCreated) ===
from maxapi import Bot, Dispatcher

app = FastAPI(title="ТПК Отслеживание")
security = HTTPBasic()

ADMIN_USER = os.environ.get("ADMIN_USER", "admin")
ADMIN_PASS = os.environ.get("ADMIN_PASS", "admin123")

DB = {}
SETTINGS = {"show_in_transit": True, "show_delivered": True, "show_with_gps": True}

# === НАСТРОЙКА БОТА MAX ===
MAX_BOT_TOKEN = "f9LHodD0cOIHWVUEeUjaeOllavxyObEdNp002Yd8vSyhrc-Rps9Z-_TyWuR5q-52_M2fEgPoLmdptzjzMWCr" 

max_bot = Bot(token=MAX_BOT_TOKEN)
max_dp = Dispatcher()

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

def save_db():
    with open("shipments.json", "w", encoding="utf-8") as f:
        json.dump(DB, f, ensure_ascii=False, indent=2)

def get_coords_from_baltgps(gps_link: str):
    if not gps_link or "maps.baltgps.ru" not in gps_link:
        return None, None, "no_link"
    try:
        token = gps_link.split("?t=")[-1]
        r = requests.get("https://maps.baltgps.ru/wialon/ajax.html", params={
            "svc": "token/login", "params": json.dumps({"token": token})
        }, timeout=(2.0, 3.0))
        
        data = r.json()
        if "error" in data: return None, None, "invalid_token"
        if "token" not in data: return None, None, "invalid_token"
        
        unit_info = json.loads(data["token"])
        items = unit_info.get("items", [])
        if not items: return None, None, "no_units"
        unit_id = items[0]
        sid = data.get("eid")
        
        r2 = requests.get("https://maps.baltgps.ru/wialon/ajax.html", params={
            "svc": "core/search_item", "params": json.dumps({"id": unit_id, "flags": 1025}), "sid": sid
        }, timeout=(2.0, 3.0))
        
        pos = r2.json().get("item", {}).get("pos", {})
        if pos and "x" in pos and "y" in pos:
            return float(pos["y"]), float(pos["x"]), "ok"
        return None, None, "no_data"
    except Exception:
        return None, None, "error"

async def fetch_single_order_coords(order_id, o):
    loop = asyncio.get_event_loop()
    lat, lng, status = await loop.run_in_executor(None, get_coords_from_baltgps, o["gps_link"])
    if lat and lng:
        o["current_lat"] = lat
        o["current_lng"] = lng
        o["has_valid_gps"] = True
    else:
        o["current_lat"] = None
        o["current_lng"] = None
        o["has_valid_gps"] = False
        o["gps_status"] = status

async def update_all_coords():
    print("⏳ Начинаю фоновую проверку GPS...")
    for o in DB.values():
        if o.get("has_valid_gps") is False:
            o["current_lat"] = None
            o["current_lng"] = None

    tasks = []
    for order_id, o in DB.items():
        if o.get("gps_link") and o.get("has_valid_gps") is not False:
            tasks.append(fetch_single_order_coords(order_id, o))
    
    semaphore = asyncio.Semaphore(15)
    async def limited_task(task):
        async with semaphore: await task
    await asyncio.gather(*(limited_task(t) for t in tasks))
    save_db()
    print("✅ Фоновая проверка GPS завершена.")

# === ОБРАБОТЧИК БОТА MAX (БЕЗ MessageCreated) ===
@max_dp.message_created()
async def handle_message(event):
    # Проверка, что это текстовое сообщение
    if not event.message or not event.message.body or not event.message.body.text:
        return

    text = event.message.body.text.strip()

    # Логика выбора заказа
    if text == "📋 Выбрать заказ":
        active_orders = [o for o in DB.values() if o.get("status") == "в пути" and o.get("current_lat")]
        if not active_orders:
            await event.message.answer("Нет активных заказов с GPS.")
            return
        
        response = "Выберите заказ (отправьте номер):\n"
        for o in active_orders[:15]:
            response += f"• {o['order_id']} ({o.get('address', '—')})\n"
        
        await event.message.answer(response)
        return

    # Если введен номер заказа
    order_id = text.upper()
    if order_id in DB:
        o = DB[order_id]
        lat, lng = o.get("current_lat"), o.get("current_lng")
        if not lat and o.get("gps_link"):
            lat, lng, _ = get_coords_from_baltgps(o["gps_link"])
            if lat:
                o["current_lat"], o["current_lng"] = lat, lng

        response = (
            f"📦 Заказ: {o['order_id']}\n"
            f"📍 Адрес: {o.get('address', '—')}\n"
            f"🚚 Перевозчик: {o.get('carrier', '—')}\n"
            f"🚛 Машина: {o.get('vehicle', '—')}\n"
            f"📊 Статус: {o.get('status', '—')}\n"
        )
        if o.get('comment'):
            response += f"📝 Примечание: {o['comment']}\n"
        
        if lat and lng:
            response += f"🗺️ [Точка на карте](https://yandex.ru/maps/?pt={lng},{lat}&z=12)"
        else:
            response += "🗺️ GPS недоступен"
            
        await event.message.answer(response)
    else:
        await event.message.answer("❌ Заказ не найден. Попробуйте снова или напишите «Выбрать заказ».")

async def run_max_bot():
    await max_dp.start_polling(max_bot)

@app.on_event("startup")
async def startup():
    global DB
    if os.path.exists("shipments.json"):
        with open("shipments.json", "r", encoding="utf-8") as f:
            DB = json.load(f)
        asyncio.create_task(update_all_coords())
    
    asyncio.create_task(run_max_bot())
    print("✅ MAX-бот запущен")

def check_admin(credentials: HTTPBasicCredentials = Depends(security)):
    if credentials.username != ADMIN_USER or credentials.password != ADMIN_PASS:
        raise HTTPException(401, "Неверный логин/пароль")
    return credentials.username

# === API ДЛЯ САЙТА ===
@app.get("/api/settings/tabs")
async def get_tabs():
    return SETTINGS

@app.post("/api/admin/settings/tabs")
async def set_tabs(s: dict, user=Depends(check_admin)):
    global SETTINGS
    SETTINGS.update(s)
    return {"ok": True}

@app.get("/api/shipments")
async def list_shipments(status: Optional[str] = None, search: Optional[str] = None, sort_by: str = "gps", force_refresh: bool = False):
    if force_refresh:
        asyncio.create_task(update_all_coords())

    result = []
    for o in DB.values():
        s = str(o.get("status", "")).lower()
        if status == "in_transit" and s not in ['в пути', 'планируется']: continue
        if status == "delivered" and s not in ['завершено', 'выгружено']: continue
        if status == "with_gps":
            if not o.get("current_lat"): continue
        
        if search:
            q = search.lower()
            if q not in o.get("order_id", "").lower() and q not in o.get("address", "").lower():
                continue
        result.append(o)
    
    def sort_key(x):
        is_valid = 1 if x.get("current_lat") else 0
        order_id = str(x.get("order_id") or "")
        city = str(x.get("address") or "").lower()
        
        if sort_by == "gps": return (not is_valid, order_id)
        elif sort_by == "order_id": return order_id
        elif sort_by == "city": return (city == "", city, order_id)
        return (not is_valid, order_id)

    result.sort(key=sort_key)
    return result

@app.get("/api/coords/{order_id}")
async def get_online_coords(order_id: str):
    if order_id not in DB: raise HTTPException(404, "Заказ не найден")
    o = DB[order_id]
    if o.get("current_lat"): return {"lat": o["current_lat"], "lng": o["current_lng"]}
    if o.get("gps_link") and o.get("has_valid_gps") is not False:
        lat, lng, status = get_coords_from_baltgps(o["gps_link"])
        if lat and lng:
            o["current_lat"] = lat
            o["current_lng"] = lng
            o["has_valid_gps"] = True
            save_db()
            return {"lat": lat, "lng": lng}
        else:
            o["has_valid_gps"] = False
            o["gps_status"] = status
            save_db()
    raise HTTPException(404, "GPS недоступен")

@app.get("/api/admin/shipments")
async def admin_list(user=Depends(check_admin)):
    return list(DB.values())

@app.post("/api/admin/shipments")
async def admin_create(s: Shipment, user=Depends(check_admin)):
    DB[s.order_id] = s.model_dump()
    DB[s.order_id]["has_valid_gps"] = None
    save_db()
    asyncio.create_task(update_all_coords())
    return {"ok": True}

@app.delete("/api/admin/shipments/{order_id}")
async def admin_delete(order_id: str, user=Depends(check_admin)):
    if order_id in DB:
        del DB[order_id]
        save_db()
    return {"ok": True}

@app.post("/api/admin/upload")
async def admin_upload_excel(file: UploadFile = File(...), user=Depends(check_admin)):
    global DB
    try:
        contents = await file.read()
        wb = openpyxl.load_workbook(io.BytesIO(contents))
        if "Исх.логистика" not in wb.sheetnames:
            raise HTTPException(400, "В файле нет листа 'Исх.логистика'")
            
        ws = wb["Исх.логистика"]
        for row_idx in range(1, ws.max_row + 1):
            if ws.row_dimensions[row_idx].hidden:
                ws.row_dimensions[row_idx].hidden = False
                
        output = io.BytesIO()
        wb.save(output)
        output.seek(0)
        
        df = pd.read_excel(output, sheet_name="Исх.логистика", dtype=str)
        
        new_db = {}
        skipped = 0
        last_orders = []
        
        for _, row in df.iterrows():
            raw_order = row.get('Заказ')
            if pd.isna(raw_order) or str(raw_order).strip() == '' or str(raw_order).strip().lower() == 'nan':
                skipped += 1
                continue
                
            order_id = str(raw_order).strip()
            last_orders.append(order_id)
            if len(last_orders) > 5: last_orders.pop(0)
            
            def fmt_date(val):
                if pd.isna(val) or str(val).strip() == '': return None
                try: return pd.to_datetime(val).strftime('%Y-%m-%d')
                except: return str(val)[:10]

            def safe_float(val):
                if pd.isna(val) or str(val).strip() == '': return None
                try: return float(str(val).replace(',', '.').strip())
                except: return None

            gps = str(row.get('ссылка')).strip() if not pd.isna(row.get('ссылка')) else None
            
            new_db[order_id] = {
                "order_id": order_id,
                "address": str(row.get('Адрес доставки')).strip() if not pd.isna(row.get('Адрес доставки')) else None,
                "weight": safe_float(row.get('вес')),
                "plan_arrival": fmt_date(row.get('Дата прибытия')),
                "carrier": str(row.get('Перевозчик')).strip() if not pd.isna(row.get('Перевозчик')) else None,
                "vehicle": str(row.get('Машина')).strip() if not pd.isna(row.get('Машина')) else None,
                "driver": str(row.get('Водитель')).strip() if not pd.isna(row.get('Водитель')) else None,
                "gps_link": gps,
                "comment": str(row.get('Примечания')).strip() if not pd.isna(row.get('Примечания')) else None,
                "status": str(row.get('статус доставки')).strip().lower() if not pd.isna(row.get('статус доставки')) else "",
                "has_valid_gps": None
            }
            
        DB = new_db
        save_db()
        asyncio.create_task(update_all_coords())
        
        debug_msg = ", ".join(last_orders)
        return {"ok": True, "message": f"Загружено {len(DB)} заказов. Пропущено пустых: {skipped}. Последние: {debug_msg}"}
    except Exception as e:
        raise HTTPException(500, f"Ошибка чтения файла: {str(e)}")

# ============ ГЛАВНАЯ СТРАНИЦА ============
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
    .controls { max-width: 1100px; margin: 0 auto 16px; display: flex; gap: 10px; flex-wrap: wrap; }
    .search-box { position: relative; flex: 1; min-width: 250px; }
    .search-box input { width: 100%; padding: 14px 20px 14px 48px; border-radius: 12px; border: 2px solid var(--border); background: var(--card); color: var(--text); }
    .search-box::before { content: '🔍'; position: absolute; left: 16px; top: 50%; transform: translateY(-50%); }
    .sort-select { padding: 14px; border-radius: 12px; border: 2px solid var(--border); background: var(--card); color: var(--text); cursor: pointer; }
    .refresh-btn { padding: 14px; border-radius: 12px; border: none; background: var(--primary); color: white; font-weight: bold; cursor: pointer; display: flex; align-items: center; gap: 6px; }
    .refresh-btn:hover { opacity: 0.9; }
    .tabs { max-width: 1100px; margin: 0 auto 24px; display: flex; gap: 8px; background: var(--card); padding: 8px; border-radius: 12px; box-shadow: var(--shadow); }
    .tab { flex: 1; padding: 12px; border: none; background: transparent; color: var(--text-sec); font-weight: 600; cursor: pointer; border-radius: 8px; display: none; }
    .tab.visible { display: block; }
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
        </select>
        <button class="refresh-btn" onclick="refreshData()" id="refreshBtn">🔄 Обновить</button>
    </div>
    <div class="tabs" id="tabsContainer">
        <button class="tab" data-status="in_transit" onclick="setTab('in_transit')">🚚 В пути</button>
        <button class="tab" data-status="delivered" onclick="setTab('delivered')">✅ Выгружено</button>
        <button class="tab" data-status="with_gps" onclick="setTab('with_gps')">📍 С GPS</button>
    </div>
    <div class="container"><div class="orders-grid" id="ordersGrid"></div></div>
    <div id="modal"><div class="modal-content"><button class="modal-close" onclick="closeModal()">×</button><div id="modalContent"></div></div></div>
    <script>
    let currentTab = 'in_transit'; let map = null;
    function toggleTheme() { const isDark = document.documentElement.getAttribute('data-theme') === 'dark'; document.documentElement.setAttribute('data-theme', isDark ? 'light' : 'dark'); localStorage.setItem('theme', isDark ? 'light' : 'dark'); }
    document.documentElement.setAttribute('data-theme', localStorage.getItem('theme') || 'light');
    
    async function loadTabs() {
        const res = await fetch('/api/settings/tabs');
        const settings = await res.json();
        document.querySelectorAll('.tab').forEach(tab => {
            const status = tab.dataset.status;
            const key = 'show_' + status;
            if (settings[key] !== false) { tab.classList.add('visible'); } else { tab.classList.remove('visible'); }
        });
        const activeTab = document.querySelector('.tab.active');
        if (activeTab && !activeTab.classList.contains('visible')) {
            const firstVisible = document.querySelector('.tab.visible');
            if (firstVisible) setTab(firstVisible.dataset.status);
        }
    }
    
    function setTab(s) { currentTab = s; document.querySelectorAll('.tab').forEach(t=>t.classList.remove('active')); document.querySelector(`.tab[data-status="${s}"]`).classList.add('active'); loadOrders(); }
    function debounceSearch() { clearTimeout(window.searchTimeout); window.searchTimeout = setTimeout(loadOrders, 300); }
    
    async function loadOrders(force_refresh = false) {
        const search = document.getElementById('searchInput').value.trim();
        const sort = document.getElementById('sortSelect').value;
        document.getElementById('ordersGrid').innerHTML = '<div style="text-align:center;width:100%;">Загрузка...</div>';
        
        const cacheParam = force_refresh ? `&force_refresh=true&_t=${Date.now()}` : '';
        const url = `/api/shipments?status=${currentTab}&sort_by=${sort}${search ? '&search='+search : ''}${cacheParam}`;
        
        try {
            const res = await fetch(url, { cache: force_refresh ? "no-store" : "default" });
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
        } catch(e) { document.getElementById('ordersGrid').innerHTML = '<div style="text-align:center;width:100%;color:red;">Ошибка загрузки</div>'; }
    }
    
    function refreshData() {
        const btn = document.getElementById('refreshBtn');
        btn.innerHTML = '⏳ Загрузка...';
        btn.disabled = true;
        loadOrders(true).finally(() => {
            btn.innerHTML = '🔄 Обновить';
            btn.disabled = false;
        });
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
            <div id="map"><div class="spinner"></div>Загрузка...</div>`;
        if (o.current_lat && o.current_lng) { renderMap(o.current_lat, o.current_lng); }
        else if (o.gps_link && o.has_valid_gps !== false) {
            try {
                const res = await fetch(`/api/coords/${o.order_id}`);
                if (res.ok) { const data = await res.json(); renderMap(data.lat, data.lng); }
                else { document.getElementById('map').innerHTML = '<div style="text-align:center;padding:50px;color:#888;">GPS отсутствует. Ориентируйтесь на примечание.</div>'; }
            } catch(e) { document.getElementById('map').innerHTML = '<div style="text-align:center;padding:50px;color:#888;">Ошибка сети.</div>'; }
        } else { document.getElementById('map').innerHTML = '<div style="text-align:center;padding:50px;color:#888;">GPS отсутствует. Ориентируйтесь на примечание.</div>'; }
    }
    function renderMap(lat, lng) {
        const mapDiv = document.getElementById('map'); mapDiv.innerHTML = '';
        if (map) map.remove();
        map = L.map('map').setView([lat, lng], 8);
        L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png').addTo(map);
        L.marker([lat, lng]).addTo(map);
    }
    function closeModal() { document.getElementById('modal').classList.remove('open'); if(map){map.remove(); map=null;} }
    
    loadTabs().then(() => loadOrders());
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
            body { font-family: Arial; padding: 20px; background: #f0f2f5; margin: 0; }
            .top-bar { display: flex; justify-content: space-between; align-items: center; margin-bottom: 20px; }
            .btn { padding: 10px 20px; border: none; border-radius: 6px; cursor: pointer; font-weight: bold; }
            .btn-green { background: #28a745; color: white; }
            .btn-blue { background: #007bff; color: white; }
            .btn-red { background: #dc3545; color: white; }
            .panel { background: white; padding: 20px; border-radius: 8px; box-shadow: 0 2px 8px rgba(0,0,0,0.1); margin-bottom: 20px; }
            .panel h3 { margin-top: 0; }
            .upload-box { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; }
            .settings-grid { display: flex; gap: 20px; margin-top: 10px; }
            .settings-grid label { display: flex; align-items: center; gap: 6px; cursor: pointer; }
            table { border-collapse: collapse; width: 100%; background: white; font-size: 13px; box-shadow: 0 2px 8px rgba(0,0,0,0.1); }
            th, td { border: 1px solid #ddd; padding: 8px; text-align: left; }
            th { background: #333; color: white; position: sticky; top: 0; }
            tr:hover { background: #f0f8ff; }
            #filter { padding: 10px; width: 300px; margin-bottom: 10px; border: 1px solid #ccc; border-radius: 6px; }
            #modal { display:none; position:fixed; top:0; left:0; width:100%; height:100%; background:rgba(0,0,0,0.5); z-index: 1000; }
            #modalContent { background:white; max-width:600px; margin:80px auto; padding:30px; border-radius:8px; max-height:80vh; overflow-y:auto; }
            #modalContent label { display:block; margin-top:10px; font-weight:bold; }
            #modalContent input, #modalContent select { width:100%; padding:8px; box-sizing:border-box; margin-top:4px; }
            .modal-buttons { margin-top:20px; text-align:right; }
            #uploadStatus { font-weight: bold; margin-left: 10px; }
        </style>
    </head>
    <body>
        <div class="top-bar">
            <h1>🚚 Управление перевозками</h1>
            <button class="btn btn-green" onclick="openAdd()">➕ Добавить заказ</button>
        </div>
        
        <div class="panel">
            <h3>👁️ Видимость вкладок на главной</h3>
            <div class="settings-grid">
                <label><input type="checkbox" id="chk_in_transit" onchange="saveTabs()"> 🚚 В пути</label>
                <label><input type="checkbox" id="chk_delivered" onchange="saveTabs()"> ✅ Выгружено</label>
                <label><input type="checkbox" id="chk_with_gps" onchange="saveTabs()"> 📍 С GPS</label>
            </div>
        </div>

        <div class="panel">
            <h3>📥 Загрузка данных из Excel</h3>
            <div class="upload-box">
                <input type="file" id="excelFile" accept=".xlsx, .xls">
                <button class="btn btn-blue" onclick="uploadExcel()" id="uploadBtn">Обновить базу из файла</button>
                <span id="uploadStatus"></span>
            </div>
            <p style="font-size: 12px; color: #666; margin-top: 10px;">Берется лист <b>"Исх.логистика"</b>. Предыдущая база полностью заменяется.</p>
        </div>

        <input id="filter" placeholder="Поиск по номеру или адресу..." oninput="renderTable()"/>
        <table id="shipments">
            <thead><tr><th>Заказ</th><th>Адрес</th><th>Перевозчик</th><th>Машина</th><th>Статус</th><th>Действия</th></tr></thead>
            <tbody></tbody>
        </table>
        <div id="modal">
            <div id="modalContent">
                <h2 id="modalTitle">Добавить заказ</h2>
                <label>Номер заказа *</label><input id="f_order_id"/>
                <label>Адрес доставки</label><input id="f_address"/>
                <label>Вес</label><input id="f_weight" type="number"/>
                <label>Перевозчик</label><input id="f_carrier"/>
                <label>Машина</label><input id="f_vehicle"/>
                <label>Водитель</label><input id="f_driver"/>
                <label>GPS-ссылка</label><input id="f_gps_link"/>
                <label>Примечание</label><input id="f_comment"/>
                <label>План. прибытие</label><input id="f_plan_arrival" type="date"/>
                <label>Статус</label>
                <select id="f_status">
                    <option value="">Без статуса</option>
                    <option value="планируется">планируется</option>
                    <option value="в пути">в пути</option>
                    <option value="выгружено">выгружено</option>
                    <option value="завершено">завершено</option>
                </select>
                <div class="modal-buttons">
                    <button class="btn" onclick="closeModal()">Отмена</button>
                    <button class="btn btn-green" onclick="saveOrder()">Сохранить</button>
                </div>
            </div>
        </div>
        <script>
        let allData=[];
        async function loadSettings() {
            const res = await fetch('/api/settings/tabs');
            const s = await res.json();
            document.getElementById('chk_in_transit').checked = s.show_in_transit !== false;
            document.getElementById('chk_delivered').checked = s.show_delivered !== false;
            document.getElementById('chk_with_gps').checked = s.show_with_gps !== false;
        }
        async function saveTabs() {
            const payload = {
                show_in_transit: document.getElementById('chk_in_transit').checked,
                show_delivered: document.getElementById('chk_delivered').checked,
                show_with_gps: document.getElementById('chk_with_gps').checked
            };
            await fetch('/api/admin/settings/tabs', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload) });
        }

        async function load(){
            const res = await fetch('/api/admin/shipments'); allData = await res.json();
            renderTable();
        }
        function renderTable(){
            const q = document.getElementById('filter').value.toLowerCase(); 
            let filtered = allData.filter(s => (s.order_id||'').toLowerCase().includes(q) || (s.address||'').toLowerCase().includes(q));
            document.querySelector('#shipments tbody').innerHTML = filtered.map(s=>`
                <tr>
                    <td><b>${s.order_id}</b></td>
                    <td>${s.address||''}</td>
                    <td>${s.carrier||''}</td>
                    <td>${s.vehicle||''}</td>
                    <td>${s.status||'—'}</td>
                    <td>
                        <button class="btn btn-blue" style="padding:4px 8px;" onclick='openEdit(${JSON.stringify(s).replace(/'/g,"&#39;")})'>✏️</button>
                        <button class="btn btn-red" style="padding:4px 8px;" onclick="del('${s.order_id}')">🗑</button>
                    </td>
                </tr>
            `).join('');
        }
        function openAdd(){ 
            ['order_id','address','weight','carrier','vehicle','driver','gps_link','comment','plan_arrival'].forEach(f=>document.getElementById('f_'+f).value=''); 
            document.getElementById('f_order_id').disabled=false;
            document.getElementById('f_status').value=''; 
            document.getElementById('modalTitle').innerText='Добавить заказ'; 
            document.getElementById('modal').style.display='block'; 
        }
        function openEdit(s){ 
            ['order_id','address','weight','carrier','vehicle','driver','gps_link','comment','plan_arrival'].forEach(f=>document.getElementById('f_'+f).value=s[f]||''); 
            document.getElementById('f_order_id').disabled=true; 
            document.getElementById('f_status').value=s.status||''; 
            document.getElementById('modalTitle').innerText='Редактировать: '+s.order_id; 
            document.getElementById('modal').style.display='block'; 
        }
        function closeModal(){ document.getElementById('modal').style.display='none'; }
        async function saveOrder(){
            const payload = { 
                order_id: document.getElementById('f_order_id').value, 
                address: document.getElementById('f_address').value, 
                weight: parseFloat(document.getElementById('f_weight').value)||null,
                carrier: document.getElementById('f_carrier').value, 
                vehicle: document.getElementById('f_vehicle').value, 
                driver: document.getElementById('f_driver').value,
                gps_link: document.getElementById('f_gps_link').value,
                comment: document.getElementById('f_comment').value,
                plan_arrival: document.getElementById('f_plan_arrival').value,
                status: document.getElementById('f_status').value 
            };
            await fetch('/api/admin/shipments',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
            closeModal(); load();
        }
        async function del(order_id){ 
            if(!confirm('Удалить '+order_id+'?')) return; 
            await fetch('/api/admin/shipments/'+order_id,{method:'DELETE'}); load(); 
        }
        
        async function uploadExcel() {
            const fileInput = document.getElementById('excelFile');
            if (!fileInput.files.length) { alert("Выберите файл!"); return; }
            
            const formData = new FormData();
            formData.append("file", fileInput.files[0]);
            
            const btn = document.getElementById('uploadBtn');
            const status = document.getElementById('uploadStatus');
            
            btn.disabled = true;
            btn.innerText = "⏳ Обработка...";
            status.innerText = "Пожалуйста, подождите...";
            status.style.color = "blue";
            
            try {
                const res = await fetch('/api/admin/upload', { method: 'POST', body: formData });
                const data = await res.json();
                if (res.ok) {
                    status.innerText = "✅ " + data.message + " (" + new Date().toLocaleTimeString() + ")";
                    status.style.color = "green";
                    load();
                } else {
                    status.innerText = "❌ Ошибка: " + data.detail;
                    status.style.color = "red";
                }
            } catch(e) {
                status.innerText = "❌ Ошибка сети";
                status.style.color = "red";
            } finally {
                btn.disabled = false;
                btn.innerText = "Обновить базу из файла";
            }
        }

        loadSettings();
        load();
        </script>
    </body>
    </html>
    """
