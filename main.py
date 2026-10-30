from fastapi import FastAPI, HTTPException, Depends, UploadFile, File
from fastapi.responses import HTMLResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from typing import Optional
from contextlib import asynccontextmanager
import os, json, requests, asyncio, io
import traceback
import logging
import secrets
import pandas as pd
import openpyxl

logging.basicConfig(level=logging.INFO, force=True)
logger = logging.getLogger("max_bot")

MAXAPI_VARIANT = None
Bot = Dispatcher = None
Command = None
InlineKeyboardBuilder = None
CallbackButton = LinkButton = None
USE_ATTACHMENTS = False

try:
    from maxapi import Bot, Dispatcher
    from maxapi.types import Command
    from maxapi.utils.inline_keyboard import InlineKeyboardBuilder
    from maxapi.types.attachments.buttons import CallbackButton, LinkButton
    USE_ATTACHMENTS = True
    MAXAPI_VARIANT = "love-apples"
    logger.info("✅ love-apples/maxapi")
except ImportError:
    from maxapi import Bot, Dispatcher, Command, InlineKeyboardBuilder
    USE_ATTACHMENTS = False
    MAXAPI_VARIANT = "sdk"
    logger.info("✅ maxapi-sdk")

from maxapi.webhook.fastapi import FastAPIMaxWebhook

def make_keyboard(callbacks: list, links: list = None):
    builder = InlineKeyboardBuilder()
    for text, payload in callbacks:
        builder.row(CallbackButton(text=text, payload=payload))
    for text, url in (links or []):
        builder.row(LinkButton(text=text, url=url))
    return builder.as_markup()

async def send_message(message, text: str, callbacks: list = None, links: list = None):
    if callbacks or links:
        try:
            kb = make_keyboard(callbacks or [], links)
            if USE_ATTACHMENTS:
                return await message.answer(text=text, attachments=[kb])
            return await message.answer(text=text, keyboard=kb)
        except Exception:
            logger.error("send with keyboard failed:\n%s", traceback.format_exc())
    return await message.answer(text=text)  # fallback без клавиатуры
ADMIN_USER = os.environ.get("ADMIN_USER", "admin")
ADMIN_PASS = os.environ.get("ADMIN_PASS", "admin123")

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

MAX_BOT_TOKEN = os.environ.get("MAX_BOT_TOKEN")
if not MAX_BOT_TOKEN:
    raise ValueError("MAX_BOT_TOKEN не задан")

PUBLIC_URL = os.environ.get("PUBLIC_URL", "").rstrip("/")
if not PUBLIC_URL:
    raise ValueError("PUBLIC_URL не задан")

WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "").strip()
if WEBHOOK_SECRET and not (5 <= len(WEBHOOK_SECRET) <= 256 and all(c.isalnum() or c in "_-" for c in WEBHOOK_SECRET)):
    raise ValueError("WEBHOOK_SECRET: 5-256 символов, только A-Za-z0-9_-")
if not WEBHOOK_SECRET:
    logger.warning("⚠️ WEBHOOK_SECRET не задан")

max_bot = Bot(token=MAX_BOT_TOKEN)
max_dp = Dispatcher()

webhook = FastAPIMaxWebhook(
    dp=max_dp,
    bot=max_bot,
    secret=WEBHOOK_SECRET if WEBHOOK_SECRET else None,
)

# === Правильный lifespan: ВКЛАДЫВАЕМ webhook.lifespan ===
@asynccontextmanager
async def lifespan(app: FastAPI):
    global DB
    # startup
    if os.path.exists("shipments.json"):
        with open("shipments.json", "r", encoding="utf-8") as f:
            DB = json.load(f)
        logger.info("DB loaded: %d orders", len(DB))
    async with webhook.lifespan(app):
        asyncio.create_task(_register_webhook_once())
        asyncio.create_task(update_all_coords())
        logger.info("✅ MAX-бот запущен (Webhook mode)")
        yield
    logger.info("🛑 Бот остановлен")

app = FastAPI(title="ТПК Отслеживание", lifespan=lifespan)
security = HTTPBasic()
os.makedirs("static", exist_ok=True)
app.mount("/static", StaticFiles(directory="static"), name="static")
webhook.setup(app, path="/webhook")

async def _register_webhook_once():
    try:
        await max_bot.delete_webhook()
        logger.info("Старая подписка удалена")
    except Exception as e:
        logger.warning("delete_webhook: %s", e)
    try:
        kwargs = {"url": f"{PUBLIC_URL}/webhook", "update_types": ["message_created", "message_callback", "bot_started"]}
        if WEBHOOK_SECRET:
            kwargs["secret"] = WEBHOOK_SECRET
        await max_bot.subscribe_webhook(**kwargs)
        logger.info("✅ Вебхук зарегистрирован: %s/webhook", PUBLIC_URL)
    except Exception as e:
        logger.error("⚠️ Ошибка регистрации вебхука: %s\n%s", e, traceback.format_exc())

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
        }, timeout=(3.0, 5.0))
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
        }, timeout=(3.0, 5.0))
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
        }, timeout=(3.0, 5.0))
        pos = r3.json().get("item", {}).get("pos", {})
        if pos and "x" in pos and "y" in pos:
            return float(pos["y"]), float(pos["x"]), "ok"
        return None, None, "no_data"
    except Exception:
        return None, None, "error"

async def fetch_single_order_coords(order_id, o):
    loop = asyncio.get_running_loop()
    lat, lng, status = await loop.run_in_executor(None, get_coords_from_baltgps, o["gps_link"])
    if lat and lng:
        o["current_lat"] = lat
        o["current_lng"] = lng
        o["has_valid_gps"] = True
    else:
        o["current_lat"] = None
        o["current_lng"] = None
        o["has_valid_gps"] = False

async def update_all_coords():
    for o in DB.values():
        if o.get("has_valid_gps") is False:
            o["current_lat"] = None
            o["current_lng"] = None
    tasks = [fetch_single_order_coords(oid, o) for oid, o in DB.items() if o.get("gps_link")]
    semaphore = asyncio.Semaphore(10)
    async def limited(t):
        async with semaphore: await t
    await asyncio.gather(*(limited(t) for t in tasks))
    save_db()

def get_main_menu():
    return [("📋 Выбрать заказ", "menu_choose_order"), ("🔄 Обновить GPS", "menu_update_gps"), ("❓ Помощь", "menu_help")]

def get_orders_menu():
    active = [o for o in DB.values() if o.get("status") == "в пути"][:10]
    items = [(f"{'🟢' if o.get('has_valid_gps') else '🔴'} {o['order_id']}", f"order_{o['order_id']}") for o in active]
    items.append(("🔙 Назад в меню", "menu_back"))
    return items
    @max_dp.bot_started()
async def handle_bot_started(event):
    try:
        await event.bot.send_message(
            chat_id=event.chat_id,
            text="👋 Добро пожаловать в бот ТПК! Отправьте номер заказа или выберите действие.",
        )
    except Exception:
        logger.error("bot_started error:\n%s", traceback.format_exc())

@max_dp.message_created(Command("start"))
async def handle_start(event):
    try:
        await send_message(event.message, "👋 Добро пожаловать в бот ТПК! Выберите действие:", get_main_menu())
    except Exception:
        logger.error("handle_start error:\n%s", traceback.format_exc())
        try:
            await event.message.answer("⚠️ Ошибка меню. Отправьте номер заказа.")
        except Exception:
            logger.error("fallback answer failed:\n%s", traceback.format_exc())

@max_dp.message_created()
async def handle_message(event):
    try:
        if not event.message or not event.message.body or not event.message.body.text:
            return
        text = event.message.body.text.strip()
        if text.startswith("/"):
            return
        found = None
        q = text.upper()
        if q.isdigit() and len(q) == 4:
            for o in DB.values():
                if o["order_id"].endswith(q):
                    found = o; break
        elif q in DB:
            found = DB[q]
        if found:
            o = found
            resp = f"📦 Заказ: {o['order_id']}\n📍 Адрес: {o.get('address') or '—'}\n🚚 Перевозчик: {o.get('carrier') or '—'}\n🚛 Машина: {o.get('vehicle') or '—'}\n📊 Статус: {o.get('status') or '—'}\n"
            if o.get('comment'): resp += f"📝 Примечание: {o['comment']}\n"
            callbacks = [("🔙 Назад в меню", "menu_back")]
            links = []
            if o.get("current_lat") and o.get("current_lng"):
                links.append(("🗺️ Открыть карту", f"https://yandex.ru/maps/?pt={o['current_lng']},{o['current_lat']}&z=12"))
            elif o.get("gps_link"):
                links.append(("🗺️ Открыть карту BaltGPS", o['gps_link']))
            else:
                resp += "\n🗺️ GPS недоступен. Попробуйте Обновить GPS."
            await send_message(event.message, resp, callbacks, links)
        else:
            await event.message.answer(f"❌ Заказ «{text}» не найден.")
    except Exception:
        logger.error("handle_message error:\n%s", traceback.format_exc())
        try: await event.message.answer("⚠️ Ошибка обработки.")
        except Exception: pass

@max_dp.message_callback()
async def handle_callback(event):
    try:
        if not event.callback or not event.callback.payload:
            return
        payload = event.callback.payload
    except Exception:
        logger.error("callback parse error:\n%s", traceback.format_exc())
        return
    try:
        await event.answer()
    except Exception:
        logger.error("event.answer failed:\n%s", traceback.format_exc())
    try:
        if payload == "menu_choose_order":
            await edit_message(event.message, "Выберите заказ:", get_orders_menu())
        elif payload == "menu_update_gps":
            await edit_message(event.message, "⏳ Обновление GPS...", get_main_menu())
            asyncio.create_task(update_all_coords())
        elif payload == "menu_help":
            help_text = "👋 Помощь\n\n🔍 Введите 4 цифры номера заказа.\n🗺️ Бот покажет статус и карту (если есть GPS)."
            await edit_message(event.message, help_text, get_main_menu())
        elif payload == "menu_back":
            await edit_message(event.message, "👋 Главное меню:", get_main_menu())
        elif payload.startswith("order_"):
            oid = payload.replace("order_", "")
            if oid in DB:
                o = DB[oid]
                resp = f"📦 Заказ: {o['order_id']}\n📍 Адрес: {o.get('address') or '—'}\n🚚 Перевозчик: {o.get('carrier') or '—'}\n🚛 Машина: {o.get('vehicle') or '—'}\n📊 Статус: {o.get('status') or '—'}\n"
                if o.get('comment'): resp += f"📝 Примечание: {o['comment']}\n"
                callbacks = [("🔙 Назад в меню", "menu_back")]
                links = []
                if o.get("current_lat") and o.get("current_lng"):
                    links.append(("🗺️ Открыть карту", f"https://yandex.ru/maps/?pt={o['current_lng']},{o['current_lat']}&z=12"))
                elif o.get("gps_link"):
                    links.append(("🗺️ Открыть карту BaltGPS", o['gps_link']))
                else:
                    resp += "\n🗺️ GPS недоступен."
                await edit_message(event.message, resp, callbacks, links)
            else:
                await event.message.answer("Заказ не найден.")
    except Exception:
        logger.error("handle_callback error:\n%s", traceback.format_exc())
        try: await event.message.answer("⚠️ Ошибка кнопки.")
        except Exception: pass

def check_admin(credentials: HTTPBasicCredentials = Depends(security)):
    if not (secrets.compare_digest(credentials.username, ADMIN_USER) and secrets.compare_digest(credentials.password, ADMIN_PASS)):
        raise HTTPException(401, "Неверный логин/пароль")
    return credentials.username
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
        if status == "with_gps" and not o.get("current_lat"): continue
        if search:
            q = search.lower()
            if q not in o.get("order_id", "").lower() and q not in o.get("address", "").lower(): continue
        result.append(o)
    def key(x):
        valid = 1 if x.get("current_lat") else 0
        oid = str(x.get("order_id") or "")
        city = str(x.get("address") or "").lower()
        if sort_by == "gps": return (not valid, oid)
        if sort_by == "order_id": return oid
        if sort_by == "city": return (city == "", city, oid)
        return (not valid, oid)
    result.sort(key=key)
    return result

@app.get("/api/coords/{order_id}")
async def get_online_coords(order_id: str):
    if order_id not in DB: raise HTTPException(404, "Заказ не найден")
    o = DB[order_id]
    if o.get("gps_link"):
        loop = asyncio.get_running_loop()
        lat, lng, status = await loop.run_in_executor(None, get_coords_from_baltgps, o["gps_link"])
        if lat and lng:
            o["current_lat"] = lat; o["current_lng"] = lng; o["has_valid_gps"] = True
            save_db()
            return {"lat": lat, "lng": lng}
        if status == "invalid_token": raise HTTPException(400, "Ссылка устарела")
        if status == "error": raise HTTPException(503, "Спутник не отвечает")
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
        del DB[order_id]; save_db()
    return {"ok": True}

@app.post("/api/admin/upload")
async def admin_upload_excel(file: UploadFile = File(...), user=Depends(check_admin)):
    global DB
    try:
        contents = await file.read()
        loop = asyncio.get_running_loop()
        def parse():
            wb = openpyxl.load_workbook(io.BytesIO(contents))
            if "Исх.логистика" not in wb.sheetnames:
                raise ValueError("Нет листа 'Исх.логистика'")
            ws = wb["Исх.логистика"]
            for i in range(1, ws.max_row + 1):
                if ws.row_dimensions[i].hidden: ws.row_dimensions[i].hidden = False
            out = io.BytesIO(); wb.save(out); out.seek(0)
            df = pd.read_excel(out, sheet_name="Исх.логистика", dtype=str)
            new_db = {}; skipped = 0
            for _, row in df.iterrows():
                raw = row.get('Заказ')
                if pd.isna(raw) or str(raw).strip() == '' or str(raw).strip().lower() == 'nan':
                    skipped += 1; continue
                oid = str(raw).strip()
                def fd(v):
                    if pd.isna(v) or str(v).strip() == '': return None
                    try: return pd.to_datetime(v).strftime('%Y-%m-%d')
                    except: return str(v)[:10]
                def sf(v):
                    if pd.isna(v) or str(v).strip() == '': return None
                    try: return float(str(v).replace(',', '.').strip())
                    except: return None
                gps = str(row.get('ссылка')).strip() if not pd.isna(row.get('ссылка')) else None
                new_db[oid] = {
                    "order_id": oid,
                    "address": str(row.get('Адрес доставки')).strip() if not pd.isna(row.get('Адрес доставки')) else None,
                    "weight": sf(row.get('вес')),
                    "plan_arrival": fd(row.get('Дата прибытия')),
                    "carrier": str(row.get('Перевозчик')).strip() if not pd.isna(row.get('Перевозчик')) else None,
                    "vehicle": str(row.get('Машина')).strip() if not pd.isna(row.get('Машина')) else None,
                    "driver": str(row.get('Водитель')).strip() if not pd.isna(row.get('Водитель')) else None,
                    "gps_link": gps,
                    "comment": str(row.get('Примечания')).strip() if not pd.isna(row.get('Примечания')) else None,
                    "status": str(row.get('статус доставки')).strip().lower() if not pd.isna(row.get('статус доставки')) else "",
                    "has_valid_gps": None
                }
            return new_db, skipped
        DB, skipped = await loop.run_in_executor(None, parse)
        save_db()
        asyncio.create_task(update_all_coords())
        return {"ok": True, "message": f"Загружено {len(DB)}. Пропущено: {skipped}"}
    except Exception as e:
        raise HTTPException(500, f"Ошибка: {e}")
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
            <img src="/static/qr.png" alt="QR-код бота MAX" style="width: 120px; height: 120px; border-radius: 8px;">
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
