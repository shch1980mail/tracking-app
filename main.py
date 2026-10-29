from fastapi import FastAPI, HTTPException, Depends, UploadFile, File
from fastapi.responses import HTMLResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel
from typing import Optional
import os, json, requests, asyncio, io, time, traceback
import pandas as pd
import openpyxl

# === ИМПОРТЫ ДЛЯ MAXAPI-SDK ===
from maxapi import Bot, Dispatcher, Command, InlineKeyboardBuilder

app = FastAPI(title="ТПК Отслеживание")
security = HTTPBasic()

ADMIN_USER = os.environ.get("ADMIN_USER", "admin")
ADMIN_PASS = os.environ.get("ADMIN_PASS", "admin123")

# === НАСТРОЙКИ ===
SETTINGS_FILE = "settings.json"
DEFAULT_SETTINGS = {"show_in_transit": True, "show_delivered": False, "show_with_gps": False}

if os.path.exists(SETTINGS_FILE):
    with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
        SETTINGS = json.load(f)
else:
    SETTINGS = DEFAULT_SETTINGS.copy()
    with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(SETTINGS, f, ensure_ascii=False, indent=2)

def save_settings():
    with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(SETTINGS, f, ensure_ascii=False, indent=2)

DB = {}

# === НАСТРОЙКА БОТА MAX ===
# ИЗМЕНЕНО: токен берётся из переменной окружения Layero (MAX_BOT_TOKEN),
# а не хардкодится. Layero прокидывает env vars в runtime-контейнер.
MAX_BOT_TOKEN = os.environ.get("MAX_BOT_TOKEN", "").strip()
if not MAX_BOT_TOKEN:
    raise RuntimeError("MAX_BOT_TOKEN не задан в переменных окружения Layero")

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

def log(*args):
    print(*args, flush=True)

# === КЭШ И БЛОКИРОВКИ GPS ===
GPS_CACHE_TTL = 120
GPS_CACHE = {}
_gps_locks = {}
_update_lock = None

def _order_lock(order_id):
    lock = _gps_locks.get(order_id)
    if lock is None:
        lock = _gps_locks[order_id] = asyncio.Lock()
    return lock

def _apply_coords(o, lat, lng):
    if lat and lng:
        o["current_lat"] = lat
        o["current_lng"] = lng
        o["has_valid_gps"] = True
    else:
        o["current_lat"] = None
        o["current_lng"] = None
        o["has_valid_gps"] = False

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
            "svc": "unit/calc_last", "params": json.dumps([unit_id]), "sid": sid
        }, timeout=(2.0, 3.0))

        pos_data = r2.json()
        if isinstance(pos_data, list) and len(pos_data) > 0:
            pos = pos_data[0]
            if "y" in pos and "x" in pos:
                return float(pos["y"]), float(pos["x"]), "ok"
        elif isinstance(pos_data, dict) and "error" not in pos_data:
            pos = pos_data.get(str(unit_id), {})
            if "y" in pos and "x" in pos:
                return float(pos["y"]), float(pos["x"]), "ok"

        r3 = requests.get("https://maps.baltgps.ru/wialon/ajax.html", params={
            "svc": "core/search_item", "params": json.dumps({"id": unit_id, "flags": 1025}), "sid": sid
        }, timeout=(2.0, 3.0))

        pos = r3.json().get("item", {}).get("pos", {})
        if pos and "x" in pos and "y" in pos:
            return float(pos["y"]), float(pos["x"]), "ok"

        return None, None, "no_data"
    except Exception:
        return None, None, "error"

async def fetch_single_order_coords(order_id, o, force=False):
    async with _order_lock(order_id):
        c = GPS_CACHE.get(order_id)
        fresh = c and c.get("link") == o.get("gps_link") and (time.time() - c["ts"]) < GPS_CACHE_TTL
        if fresh and not force:
            _apply_coords(o, c["lat"], c["lng"])
            return
        loop = asyncio.get_running_loop()
        # GPS-запрос в отдельном потоке, чтобы не блокировать event loop
        lat, lng, status = await loop.run_in_executor(None, get_coords_from_baltgps, o["gps_link"])
        GPS_CACHE[order_id] = {"ts": time.time(), "link": o.get("gps_link"), "lat": lat, "lng": lng}
        _apply_coords(o, lat, lng)

async def update_all_coords(force=False):
    global _update_lock
    if _update_lock is None:
        _update_lock = asyncio.Lock()
    if _update_lock.locked():
        log("[GPS] обновление уже выполняется — повторный запуск пропущен")
        return False
    async with _update_lock:
        for o in DB.values():
            if o.get("has_valid_gps") is False:
                o["current_lat"] = None
                o["current_lng"] = None

        semaphore = asyncio.Semaphore(10)
        async def limited(order_id, o):
            async with semaphore:
                await fetch_single_order_coords(order_id, o, force=force)

        items = [(oid, o) for oid, o in DB.items() if o.get("gps_link")]
        results = await asyncio.gather(*(limited(oid, o) for oid, o in items), return_exceptions=True)
        for (oid, _), r in zip(items, results):
            if isinstance(r, Exception):
                log(f"[GPS] {oid}: ошибка {r!r}")
        save_db()
        log(f"[GPS] обновлено заказов: {len(items)}")
        return True

# === ФУНКЦИИ ДЛЯ КЛАВИАТУР (maxapi-sdk) ===
def get_main_menu():
    builder = InlineKeyboardBuilder()
    builder.callback("📋 Выбрать заказ", "menu_choose_order")
    builder.callback("🔄 Обновить GPS", "menu_update_gps")
    builder.callback("❓ Помощь", "menu_help")
    return builder

def get_orders_menu():
    active_orders = [o for o in DB.values() if o.get("status") == "в пути"][:10]
    builder = InlineKeyboardBuilder()
    for o in active_orders:
        gps_mark = "🟢" if o.get("has_valid_gps") else "🔴"
        builder.callback(f"{gps_mark} {o['order_id']}", f"order_{o['order_id']}")
    builder.callback("🔙 Назад в меню", "menu_back")
    return builder

# === ЛОГИКА БОТА MAX ===
@max_dp.message_created(Command("start"))
async def handle_start(event):
    try:
        await asyncio.wait_for(
            event.message.answer(
                text="👋 Добро пожаловать в бот ТПК! Выберите действие:",
                keyboard=get_main_menu()
            ),
            timeout=10.0
        )
    except asyncio.TimeoutError:
        log("[START] таймаут отправки")
    except Exception as e:
        log(f"[START] ошибка: {e!r}")

@max_dp.message_created()
async def handle_message(event):
    if not event.message or not event.message.body or not event.message.body.text:
        return

    text = event.message.body.text.strip()
    if text.startswith("/"):
        return

    found_order = None
    search_query = text.upper()

    if search_query.isdigit() and len(search_query) == 4:
        for o in DB.values():
            if o["order_id"].endswith(search_query):
                found_order = o
                break
    else:
        if search_query in DB:
            found_order = DB[search_query]

    if found_order:
        o = found_order
        response = (
            f"📦 **Заказ:** {o['order_id']}\n"
            f"📍 **Адрес:** {o.get('address') or '—'}\n"
            f"🚚 **Перевозчик:** {o.get('carrier') or '—'}\n"
            f"🚛 **Машина:** {o.get('vehicle') or '—'}\n"
            f"📊 **Статус:** {o.get('status') or '—'}\n"
        )
        if o.get('comment'):
            response += f"📝 **Примечание:** {o['comment']}\n"

        builder = InlineKeyboardBuilder()
        if o.get("current_lat") and o.get("current_lng"):
            map_url = f"https://yandex.ru/maps/?pt={o['current_lng']},{o['current_lat']}&z=12"
            builder.link("🗺️ Открыть карту", map_url)
        elif o.get("gps_link"):
            builder.link("🗺️ Открыть карту BaltGPS", o['gps_link'])
        else:
            response += "\n🗺️ GPS недоступен. Попробуйте **🔄 Обновить GPS**."

        builder.callback("🔙 Назад в меню", "menu_back")

        try:
            # ИЗМЕНЕНО: wait_for защищает от зависания, если MAX API не отвечает
            await asyncio.wait_for(
                event.message.answer(text=response, keyboard=builder),
                timeout=10.0
            )
        except asyncio.TimeoutError:
            log("[MESSAGE] таймаут отправки карточки заказа")
        except Exception as e:
            log(f"[MESSAGE] ошибка отправки: {e!r}")
    else:
        try:
            await asyncio.wait_for(
                event.message.answer(f"❌ Заказ по запросу «{text}» не найден. Проверьте номер или выберите действие из меню."),
                timeout=10.0
            )
        except asyncio.TimeoutError:
            log("[MESSAGE] таймаут отправки 'не найден'")
        except Exception as e:
            log(f"[MESSAGE] ошибка: {e!r}")

# === ОБРАБОТКА НАЖАТИЙ КНОПОК ===
@max_dp.message_callback()
async def handle_callback(event):
    if not event.callback or not event.callback.payload:
        return

    payload = event.callback.payload
    try:
        await asyncio.wait_for(event.answer(), timeout=5.0)
    except Exception:
        pass

    try:
        if payload == "menu_choose_order":
            await asyncio.wait_for(
                event.message.edit(text="Выберите заказ из списка:", keyboard=get_orders_menu()),
                timeout=10.0
            )
        elif payload == "menu_update_gps":
            await asyncio.wait_for(
                event.message.edit(
                    text="⏳ Запускаю обновление GPS в фоне. Это может занять минуту...",
                    keyboard=get_main_menu()
                ),
                timeout=10.0
            )
            asyncio.create_task(update_all_coords(force=True))
        elif payload == "menu_help":
            help_text = (
                "👋 **Помощь**\n\n"
                "🔍 **Как искать:**\n"
                "• Введите **4 цифры** номера заказа (например, `1668`).\n"
                "• Или нажмите **📋 Выбрать заказ**, чтобы увидеть список.\n\n"
                "🗺️ **Что я умею:**\n"
                "• Показывать адрес, перевозчика, машину и статус.\n"
                "• Отправлять точку на карте (если есть GPS).\n"
                "• Показывать примечания (если GPS нет)."
            )
            await asyncio.wait_for(
                event.message.edit(text=help_text, keyboard=get_main_menu()),
                timeout=10.0
            )
        elif payload == "menu_back":
            await asyncio.wait_for(
                event.message.edit(text="👋 Главное меню:", keyboard=get_main_menu()),
                timeout=10.0
            )
        elif payload.startswith("order_"):
            order_id = payload.replace("order_", "")
            if order_id in DB:
                o = DB[order_id]
                response = (
                    f"📦 **Заказ:** {o['order_id']}\n"
                    f"📍 **Адрес:** {o.get('address') or '—'}\n"
                    f"🚚 **Перевозчик:** {o.get('carrier') or '—'}\n"
                    f"🚛 **Машина:** {o.get('vehicle') or '—'}\n"
                    f"📊 **Статус:** {o.get('status') or '—'}\n"
                )
                if o.get('comment'):
                    response += f"📝 **Примечание:** {o['comment']}\n"

                builder = InlineKeyboardBuilder()
                if o.get("current_lat") and o.get("current_lng"):
                    map_url = f"https://yandex.ru/maps/?pt={o['current_lng']},{o['current_lat']}&z=12"
                    builder.link("🗺️ Открыть карту", map_url)
                elif o.get("gps_link"):
                    builder.link("🗺️ Открыть карту BaltGPS", o['gps_link'])
                else:
                    response += "\n🗺️ GPS недоступен. Попробуйте **🔄 Обновить GPS**."

                builder.callback("🔙 Назад в меню", "menu_back")
                await asyncio.wait_for(
                    event.message.edit(text=response, keyboard=builder),
                    timeout=10.0
                )
            else:
                await asyncio.wait_for(
                    event.message.answer("Заказ не найден."),
                    timeout=10.0
                )
    except asyncio.TimeoutError:
        log(f"[CALLBACK] таймаут обработки payload={payload!r}")
    except Exception as e:
        log(f"[CALLBACK] ошибка payload={payload!r}: {e!r}")

# === WATCHDOG ДЛЯ POLLING ===
_last_event_time = time.time()

async def polling_watchdog():
    global _last_event_time
    while True:
        await asyncio.sleep(60)
        idle = time.time() - _last_event_time
        if idle > 300:
            log(f"[WATCHDOG] polling не получал событий {idle:.0f} сек — перезапускаю")
            try:
                await max_dp.stop_polling()
            except Exception:
                pass
            _last_event_time = time.time()

async def update_event_time(handler, event, data):
    global _last_event_time
    _last_event_time = time.time()
    return await handler(event, data)

max_dp.use(update_event_time)

async def run_max_bot():
    # ИЗМЕНЕНО: при InvalidToken бот останавливается, а не крутит бесконечный цикл.
    # Для остальных ошибок — перезапуск с задержкой.
    while True:
        try:
            await max_bot.delete_webhook()
            log("[BOT] start_polling")
            await max_dp.start_polling(max_bot)
            break
        except asyncio.CancelledError:
            raise
        except Exception as e:
            err_str = str(e).lower()
            if "invalid access_token" in err_str or "неверный токен" in err_str or "invalidtoken" in err_str:
                log(f"❌ Фатальная ошибка токена MAX: {e}. Бот остановлен. Проверьте MAX_BOT_TOKEN в Layero.")
                raise
            log(f"❌ MAX polling упал: {e!r}")
            traceback.print_exc()
            await asyncio.sleep(10)

@app.on_event("startup")
async def startup():
    global DB
    if os.path.exists("shipments.json"):
        with open("shipments.json", "r", encoding="utf-8") as f:
            DB = json.load(f)
        asyncio.create_task(update_all_coords())

    asyncio.create_task(run_max_bot())
    asyncio.create_task(polling_watchdog())
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
    save_settings()
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

    if o.get("gps_link"):
        loop = asyncio.get_running_loop()
        lat, lng, status = await loop.run_in_executor(None, get_coords_from_baltgps, o["gps_link"])
        if lat and lng:
            o["current_lat"] = lat
            o["current_lng"] = lng
            o["has_valid_gps"] = True
            GPS_CACHE[order_id] = {"ts": time.time(), "link": o.get("gps_link"), "lat": lat, "lng": lng}
            save_db()
            return {"lat": lat, "lng": lng}
        else:
            if status == "invalid_token":
                raise HTTPException(400, "Ссылка на GPS устарела")
            elif status == "error":
                raise HTTPException(503, "Спутник не отвечает")

    if o.get("current_lat"): return {"lat": o["current_lat"], "lng": o["current_lng"]}
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
    .modal-close { position: absolute; top: 16px; right: 16px; font-size: 24px; cursor: pointer; border: none; background: none; color: var(--text); }
    #map { height: 400px; margin-top: 20px; border-radius: 12px; display: flex; flex-direction: column; align-items: center; justify-content: center; background: #eaeaea; color: #888; gap: 10px; }
    [data-theme="dark"] #map { background: #1a1a1a; }
    .modal-info { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; margin-top: 20px; }
    .modal-info strong { display: block; color: var(--text-sec); font-size: 12px; text-transform: uppercase; }
    .spinner { border: 4px solid #f3f3f3; border-top: 4px solid #3498db; border-radius: 50%; width: 30px; height: 30px; animation: spin 1s linear infinite; margin-bottom: 10px; }
    @keyframes spin { 0% { transform: rotate(0deg); } 100% { transform: rotate(360deg); } }
    .btn-reload { padding: 10px 20px; background: var(--primary); color: white; border: none; border-radius: 8px; cursor: pointer; font-weight: bold; text-decoration: none; display: inline-block; }
</style>
</head>
<body>
    <div class="header"><h1>📍 ТПК — Отслеживание</h1><button class="theme-toggle" onclick="toggleTheme()" style="background:none;border:none;font-size:24px;cursor:pointer;">🌙</button></div>

    <div style="max-width: 1100px; margin: 0 auto 32px; background: linear-gradient(135deg, #1e3a8a 0%, #3b82f6 100%); padding: 30px; border-radius: 16px; box-shadow: 0 10px 25px -5px rgba(59, 130, 246, 0.5); display: flex; flex-wrap: wrap; align-items: center; justify-content: space-between; gap: 30px; color: white;">
        <div style="flex: 1; min-width: 280px;">
            <h2 style="margin-bottom: 12px; font-size: 24px; font-weight: 700; display: flex; align-items: center; gap: 10px;">
                <span style="font-size: 32px;">🤖</span> Отслеживайте груз в MAX
            </h2>
            <p style="font-size: 16px; opacity: 0.9; margin-bottom: 24px; line-height: 1.5;">
                Наш умный бот в мессенджере MAX мгновенно покажет статус заказа и точку на карте. Просто отправьте ему номер заказа (например, <b style="background: rgba(255,255,255,0.2); padding: 2px 6px; border-radius: 4px;">1668</b>).
            </p>
            <a href="https://max.ru/se14795631_bot" target="_blank" style="background: white; color: #1e3a8a; padding: 14px 28px; text-decoration: none; border-radius: 10px; font-weight: 700; font-size: 16px; display: inline-flex; align-items: center; gap: 8px; transition: all 0.2s; box-shadow: 0 4px 6px rgba(0,0,0,0.1);">
                🚀 Запустить бота
            </a>
        </div>
        <div style="text-align: center; background: white; padding: 15px; border-radius: 12px; box-shadow: 0 4px 15px rgba(0,0,0,0.2);">
            <img src="data:image/png;base64,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" alt="QR-код бота MAX" style="width: 120px; height: 120px; border-radius: 8px;">
            <p style="font-size: 11px; color: #64748b; margin-top: 10px; font-weight: 600;">Наведите камеру телефона</p>
        </div>
    </div>

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
            <div id="map"><div class="spinner"></div>Загрузка координат...</div>`;

        if (o.current_lat && o.current_lng) {
            renderMap(o.current_lat, o.current_lng);
        } else if (o.gps_link) {
            loadCoords(o.order_id, o.gps_link);
        } else {
            document.getElementById('map').innerHTML = '<div style="text-align:center;padding:50px;color:#888;">GPS отсутствует. Ориентируйтесь на примечание.</div>';
        }
    }

    async function loadCoords(orderId, gpsLink) {
        const mapDiv = document.getElementById('map');
        mapDiv.innerHTML = '<div class="spinner"></div><div>Запрос к спутнику...</div>';
        try {
            const res = await fetch(`/api/coords/${orderId}`);
            if (res.ok) {
                const data = await res.json();
                renderMap(data.lat, data.lng);
            } else {
                const errorData = await res.json();
                mapDiv.innerHTML = `<div style="text-align:center;padding:20px;color:#888;">
                    ${errorData.detail || 'GPS недоступен'}<br><br>
                    <a href="${gpsLink}" target="_blank" class="btn-reload">🗺️ Открыть карту BaltGPS</a>
                </div>`;
            }
        } catch(e) {
            mapDiv.innerHTML = `<div style="text-align:center;padding:20px;color:#888;">
                Ошибка сети<br><br>
                <a href="${gpsLink}" target="_blank" class="btn-reload">🗺️ Открыть карту BaltGPS</a>
            </div>`;
        }
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
