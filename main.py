import os
import json
import time
import asyncio
import secrets
import shutil
import traceback
import io
import re
from contextlib import asynccontextmanager
from typing import Optional

import pandas as pd
import openpyxl
import requests
from fastapi import FastAPI, HTTPException, Depends, UploadFile, File, Request
from fastapi.responses import HTMLResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# === MAX API ===
from maxapi import Bot, Dispatcher
from maxapi.types import TextFormat
from maxapi.filters.command import Command
from maxapi.utils.inline_keyboard import InlineKeyboardBuilder
from maxapi.webhook.fastapi import FastAPIMaxWebhook

# ============================================================================
# КОНФИГУРАЦИЯ (берется из переменных окружения Layero)
# ============================================================================
DATA_DIR = os.environ.get("DATA_DIR", ".").strip() or "."
DB_FILE = os.path.join(DATA_DIR, "shipments.json")
SETTINGS_FILE = os.path.join(DATA_DIR, "settings.json")

ADMIN_USER = os.environ.get("ADMIN_USER", "").strip()
ADMIN_PASS = os.environ.get("ADMIN_PASS", "").strip()
if not ADMIN_USER or len(ADMIN_PASS) < 8:
    raise RuntimeError("ADMIN_USER и ADMIN_PASS (>=8 символов) должны быть заданы в Layero")

MAX_BOT_TOKEN = os.environ.get("MAX_BOT_TOKEN", "").strip()
if not MAX_BOT_TOKEN:
    raise RuntimeError("MAX_BOT_TOKEN не задан в Layero")

# Настройки Webhook
WEBHOOK_PATH = "/webhook/max"
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "").strip()
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "").strip()

if not WEBHOOK_URL.startswith("https://"):
    raise RuntimeError("WEBHOOK_URL должен начинаться с https:// (MAX не принимает http)")
if len(WEBHOOK_SECRET) < 16:
    raise RuntimeError("WEBHOOK_SECRET (>=16 символов) должен быть задан в Layero")

GPS_CACHE_TTL = 120
GPS_ERROR_TTL = 30
GPS_REFRESH_INTERVAL = int(os.environ.get("GPS_REFRESH_INTERVAL", "300"))
MANUAL_REFRESH_COOLDOWN = 60
MAX_UPLOAD = 10 * 1024 * 1024
FINAL_STATUSES = {"завершено", "выгружено"}

DEFAULT_SETTINGS = {"show_in_transit": True, "show_delivered": False, "show_with_gps": False}

# ============================================================================
# ЛОГИ И БАЗА ДАННЫХ
# ============================================================================
def log(*args):
    print(*args, flush=True)

def safe_repr(e) -> str:
    return repr(e).replace(MAX_BOT_TOKEN, "***")

def load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default
    except Exception as e:
        bad = f"{path}.corrupt-{int(time.time())}"
        try:
            shutil.move(path, bad)
        except OSError:
            pass
        log(f"[DB] {path} повреждён ({type(e).__name__}), сохранён как {bad}, старт с дефолтом")
        return default

def atomic_write(path, text):
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)

DB = load_json(DB_FILE, {})
SETTINGS = {**DEFAULT_SETTINGS, **load_json(SETTINGS_FILE, {})}

def save_settings():
    atomic_write(SETTINGS_FILE, json.dumps(SETTINGS, ensure_ascii=False, indent=2))

_save_lock = asyncio.Lock()

async def save_db_async():
    async with _save_lock:
        payload = json.dumps(DB, ensure_ascii=False)
        await asyncio.to_thread(atomic_write, DB_FILE, payload)

# ============================================================================
# МОДЕЛИ ДАННЫХ
# ============================================================================
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

class TabsSettings(BaseModel):
    show_in_transit: Optional[bool] = None
    show_delivered: Optional[bool] = None
    show_with_gps: Optional[bool] = None

PUBLIC_FIELDS = ("order_id", "address", "weight", "plan_arrival", "carrier",
                 "vehicle", "status", "comment", "current_lat", "current_lng",
                 "gps_updated_at", "gps_status")

def public_view(o: dict) -> dict:
    d = {k: o.get(k) for k in PUBLIC_FIELDS}
    d["can_refresh"] = bool(o.get("gps_link"))
    return d

# ============================================================================
# GPS ЛОГИКА
# ============================================================================
GPS_CACHE = {}
_gps_locks = {}
_update_lock = None
_last_manual_refresh = 0.0
_session = requests.Session()

def _order_lock(order_id):
    lock = _gps_locks.get(order_id)
    if lock is None:
        lock = _gps_locks[order_id] = asyncio.Lock()
    return lock

def _apply_coords(o, lat, lng, status="ok"):
    if lat is not None and lng is not None:
        o["current_lat"] = lat
        o["current_lng"] = lng
        o["has_valid_gps"] = True
        o["gps_status"] = "ok"
        o["gps_updated_at"] = int(time.time())
    else:
        o["gps_status"] = status
        if status not in ("error", "timeout", "no_data"):
            o["current_lat"] = None
            o["current_lng"] = None
            o["has_valid_gps"] = False

def get_coords_from_baltgps(gps_link: str):
    if not gps_link or "maps.baltgps.ru" not in gps_link:
        return None, None, "no_link"
    try:
        token = gps_link.split("?t=")[-1]
        r = _session.get("https://maps.baltgps.ru/wialon/ajax.html", params={
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
        r2 = _session.get("https://maps.baltgps.ru/wialon/ajax.html", params={
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
        r3 = _session.get("https://maps.baltgps.ru/wialon/ajax.html", params={
            "svc": "core/search_item", "params": json.dumps({"id": unit_id, "flags": 1025}), "sid": sid
        }, timeout=(2.0, 3.0))
        pos = r3.json().get("item", {}).get("pos", {})
        if pos and "x" in pos and "y" in pos:
            return float(pos["y"]), float(pos["x"]), "ok"
        return None, None, "no_data"
    except requests.Timeout:
        return None, None, "timeout"
    except Exception as e:
        log(f"[GPS] ошибка запроса: {type(e).__name__}")
        return None, None, "error"

async def fetch_single_order_coords(order_id, o, force=False):
    async with _order_lock(order_id):
        c = GPS_CACHE.get(order_id)
        ttl = GPS_CACHE_TTL if (c and c.get("status") == "ok") else GPS_ERROR_TTL
        if c and not force and c.get("link") == o.get("gps_link") and time.time() - c["ts"] < ttl:
            _apply_coords(o, c["lat"], c["lng"], c["status"])
            return
        loop = asyncio.get_running_loop()
        lat, lng, status = await loop.run_in_executor(None, get_coords_from_baltgps, o.get("gps_link"))
        GPS_CACHE[order_id] = {"ts": time.time(), "link": o.get("gps_link"),
                               "lat": lat, "lng": lng, "status": status}
        _apply_coords(o, lat, lng, status)

async def _update_pass(force):
    sem = asyncio.Semaphore(10)
    async def one(oid, o):
        async with sem:
            await fetch_single_order_coords(oid, o, force=force)
    items = [(oid, o) for oid, o in list(DB.items())
             if o.get("gps_link") and str(o.get("status") or "").lower() not in FINAL_STATUSES]
    results = await asyncio.gather(*(one(*it) for it in items), return_exceptions=True)
    for (oid, _), r in zip(items, results):
        if isinstance(r, Exception):
            log(f"[GPS] {oid}: {type(r).__name__}")
    await save_db_async()
    log(f"[GPS] проход завершён, заказов: {len(items)}")

async def update_all_coords(force=False):
    global _update_lock
    if _update_lock is None:
        _update_lock = asyncio.Lock()
    if _update_lock.locked():
        log("[GPS] обновление уже выполняется — пропуск")
        return False
    async with _update_lock:
        await _update_pass(force)
        return True

async def gps_refresher():
    while True:
        try:
            await update_all_coords()
        except Exception as e:
            log(f"[GPS] refresher: {type(e).__name__}")
        await asyncio.sleep(GPS_REFRESH_INTERVAL)

def allow_manual_refresh() -> bool:
    global _last_manual_refresh
    now = time.time()
    if now - _last_manual_refresh < MANUAL_REFRESH_COOLDOWN:
        return False
    _last_manual_refresh = now
    return True

# ============================================================================
# ФОНОВЫЕ ЗАДАЧИ
# ============================================================================
_bg_tasks = set()

def spawn(coro, name=None):
    t = asyncio.create_task(coro, name=name)
    _bg_tasks.add(t)
    def _done(t):
        _bg_tasks.discard(t)
        if not t.cancelled() and t.exception():
            log(f"[TASK {t.get_name()}] упала: {type(t.exception()).__name__}")
    t.add_done_callback(_done)
    return t

# ============================================================================
# БОТ MAX (ОБРАБОТЧИКИ)
# ============================================================================
HTML_FMT = TextFormat.HTML
max_bot = Bot(token=MAX_BOT_TOKEN)
max_dp = Dispatcher()

def esc_md(v):
    if v in (None, ""):
        return "—"
    return str(v).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

def get_main_menu():
    b = InlineKeyboardBuilder()
    b.callback("📋 Выбрать заказ", "menu_choose_order")
    b.callback("🔄 Обновить GPS", "menu_update_gps")
    b.callback("❓ Помощь", "menu_help")
    return b

def get_orders_menu():
    active = [o for o in DB.values()
              if str(o.get("status") or "").lower() in ("в пути", "планируется")][:10]
    b = InlineKeyboardBuilder()
    for o in active:
        mark = "🟢" if o.get("has_valid_gps") else "🔴"
        b.callback(f"{mark} {o['order_id']}", f"order_{o['order_id']}")
    b.callback("🔙 Назад в меню", "menu_back")
    return b

def render_order_card(o):
    text = (
        f"📦 <b>Заказ:</b> {esc_md(o.get('order_id'))}\n"
        f"📍 <b>Адрес:</b> {esc_md(o.get('address'))}\n"
        f"🚚 <b>Перевозчик:</b> {esc_md(o.get('carrier'))}\n"
        f"🚛 <b>Машина:</b> {esc_md(o.get('vehicle'))}\n"
        f"📊 <b>Статус:</b> {esc_md(o.get('status'))}\n"
    )
    if o.get("comment"):
        text += f"📝 <b>Примечание:</b> {esc_md(o['comment'])}\n"
    kb = InlineKeyboardBuilder()
    if o.get("current_lat") is not None and o.get("current_lng") is not None:
        kb.link("🗺️ Открыть карту",
                f"https://yandex.ru/maps/?pt={o['current_lng']},{o['current_lat']}&z=12")
        if o.get("gps_updated_at"):
            text += f"🕒 Обновлено: {time.strftime('%d.%m %H:%M', time.localtime(o['gps_updated_at']))}\n"
    else:
        text += "\n🗺️ GPS недоступен. Попробуйте «🔄 Обновить GPS»."
    kb.callback("🔙 Назад в меню", "menu_back")
    return text, kb

def find_orders(query: str):
    q = query.strip().upper()
    exact = [o for oid, o in DB.items() if oid.upper() == q]
    if exact:
        return exact
    if q.isdigit() and len(q) == 4:
        return [o for oid, o in DB.items() if oid.upper().endswith(q)]
    return []

async def safe_send(coro, tag, timeout=10.0):
    try:
        return await asyncio.wait_for(coro, timeout)
    except asyncio.TimeoutError:
        log(f"[{tag}] таймаут")
    except Exception as e:
        log(f"[{tag}] ошибка: {safe_repr(e)}")

@max_dp.message_created(Command("start"))
async def handle_start(event):
    await safe_send(
        event.message.answer(text="👋 Добро пожаловать в бот ТПК! Выберите действие:",
                             keyboard=get_main_menu(), format=HTML_FMT),
        "START"
    )

@max_dp.message_created()
async def handle_message(event):
    if not event.message or not event.message.body or not event.message.body.text:
        return
    text = event.message.body.text.strip()[:100]
    if not text or text.startswith("/"):
        return
    found = find_orders(text)
    if len(found) == 1:
        t, kb = render_order_card(found[0])
        await safe_send(event.message.answer(text=t, keyboard=kb, format=HTML_FMT), "MESSAGE")
    elif len(found) > 1:
        kb = InlineKeyboardBuilder()
        for o in found[:10]:
            kb.callback(o["order_id"], f"order_{o['order_id']}")
        await safe_send(
            event.message.answer(text=f"🔎 Найдено {len(found)}. Выберите:", keyboard=kb, format=HTML_FMT),
            "MESSAGE"
        )
    else:
        await safe_send(
            event.message.answer(f"❌ Заказ по запросу «{text}» не найден.", format=HTML_FMT),
            "MESSAGE"
        )

@max_dp.message_callback()
async def handle_callback(event):
    if not event.callback or not event.callback.payload:
        return
    payload = event.callback.payload
    try:
        await asyncio.wait_for(event.answer(), timeout=5.0)
    except Exception:
        pass

    if payload == "menu_choose_order":
        await safe_send(event.message.edit(text="Выберите заказ:", keyboard=get_orders_menu(), format=HTML_FMT), "CALLBACK")
    elif payload == "menu_update_gps":
        if allow_manual_refresh():
            spawn(update_all_coords(force=True), "gps-bot")
            txt = "⏳ Обновляю GPS в фоне (до минуты). Откройте заказ снова."
        else:
            txt = "ℹ️ GPS обновлялся меньше минуты назад."
        await safe_send(event.message.edit(text=txt, keyboard=get_main_menu(), format=HTML_FMT), "CALLBACK")
    elif payload == "menu_help":
        help_text = (
            "👋 <b>Помощь</b>\n\n"
            "🔍 <b>Как искать:</b>\n"
            "• Введите 4 цифры номера заказа (например, 1668).\n"
            "• Или нажмите «📋 Выбрать заказ».\n\n"
            "🗺️ <b>Что я умею:</b>\n"
            "• Показывать адрес, перевозчика, машину и статус.\n"
            "• Отправлять точку на карте (если есть GPS).\n"
            "• Показывать примечания (если GPS нет)."
        )
        await safe_send(event.message.edit(text=help_text, keyboard=get_main_menu(), format=HTML_FMT), "CALLBACK")
    elif payload == "menu_back":
        await safe_send(event.message.edit(text="👋 Главное меню:", keyboard=get_main_menu(), format=HTML_FMT), "CALLBACK")
    elif payload.startswith("order_"):
        order_id = payload.removeprefix("order_")
        o = DB.get(order_id)
        if not o:
            await safe_send(event.message.answer("Заказ не найден.", format=HTML_FMT), "CALLBACK")
            return
        t, kb = render_order_card(o)
        await safe_send(event.message.edit(text=t, keyboard=kb, format=HTML_FMT), "CALLBACK")

# ============================================================================
# WEBHOOK И LIFESPAN (ЗАМЕНА POLLING)
# ============================================================================
webhook = FastAPIMaxWebhook(dp=max_dp, bot=max_bot)

@asynccontextmanager
async def lifespan(app: FastAPI):
    # 1. Загружаем БД при старте
    global DB
    DB = load_json(DB_FILE, {})
    
    # 2. Запускаем фоновые задачи (GPS)
    spawn(gps_refresher(), "gps-refresher")
    
    # 3. Подписываем бота на Webhook (если ещё не подписан)
    try:
        await max_bot.subscribe_webhook(url=WEBHOOK_URL, secret=WEBHOOK_SECRET)
        log(f"[WEBHOOK] Бот подписан на {WEBHOOK_URL}")
    except Exception as e:
        log(f"[WEBHOOK] Ошибка подписки (возможно, уже подписан): {e}")

    # 4. Работаем внутри lifespan вебхука
    async with webhook.lifespan(app):
        yield
        
    # 5. Очистка при выключении
    for t in list(_bg_tasks):
        t.cancel()

app = FastAPI(title="ТПК Отслеживание", lifespan=lifespan)
webhook.setup(app, path=WEBHOOK_PATH)

# Статика (для QR-кода)
os.makedirs("static", exist_ok=True)
app.mount("/static", StaticFiles(directory="static"), name="static")

# ============================================================================
# АДМИН-ДОСТУП И API ДЛЯ САЙТА
# ============================================================================
security = HTTPBasic()
_fail = {}

def check_admin(request: Request, credentials: HTTPBasicCredentials = Depends(security)):
    ip = request.client.host if request.client else "?"
    cnt, ts = _fail.get(ip, (0, time.time()))
    if time.time() - ts > 600:
        cnt, ts = 0, time.time()
    if cnt >= 5:
        raise HTTPException(429, "Слишком много попыток, подождите")
    ok = (secrets.compare_digest(credentials.username.encode(), ADMIN_USER.encode())
          & secrets.compare_digest(credentials.password.encode(), ADMIN_PASS.encode()))
    if not ok:
        _fail[ip] = (cnt + 1, ts)
        raise HTTPException(401, "Неверный логин/пароль", headers={"WWW-Authenticate": "Basic"})
    _fail.pop(ip, None)
    return credentials.username

@app.get("/api/settings/tabs")
async def get_tabs():
    return SETTINGS

@app.post("/api/admin/settings/tabs")
async def set_tabs(s: TabsSettings, user=Depends(check_admin)):
    SETTINGS.update(s.model_dump(exclude_none=True))
    save_settings()
    return {"ok": True}

@app.get("/api/shipments")
async def list_shipments(status: Optional[str] = None, search: Optional[str] = None,
                         sort_by: str = "gps", force_refresh: bool = False):
    if force_refresh and allow_manual_refresh():
        spawn(update_all_coords(), "manual-refresh")
    result = []
    for o in DB.values():
        s = str(o.get("status") or "").lower()
        if status == "in_transit" and s not in ("в пути", "планируется"): continue
        if status == "delivered" and s not in ("завершено", "выгружено"): continue
        if status == "with_gps" and o.get("current_lat") is None: continue
        if search:
            q = search.lower()
            hay = " ".join(str(o.get(k) or "") for k in
                           ("order_id", "address", "vehicle", "carrier")).lower()
            if q not in hay:
                continue
        result.append(public_view(o))
    def sort_key(x):
        is_valid = 1 if x.get("current_lat") is not None else 0
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
    o = DB.get(order_id)
    if not o:
        raise HTTPException(404, "Заказ не найден")
    if o.get("gps_link"):
        await fetch_single_order_coords(order_id, o)
        if o.get("current_lat") is not None:
            spawn(save_db_async(), "save")
            return {"lat": o["current_lat"], "lng": o["current_lng"],
                    "updated_at": o.get("gps_updated_at")}
        if o.get("gps_status") == "invalid_token":
            raise HTTPException(400, "Ссылка на GPS устарела")
    raise HTTPException(404, "GPS недоступен")

@app.get("/api/admin/shipments")
async def admin_list(user=Depends(check_admin)):
    return list(DB.values())

@app.post("/api/admin/shipments")
async def admin_create(s: Shipment, user=Depends(check_admin)):
    oid = s.order_id.strip()
    if not oid:
        raise HTTPException(422, "Пустой номер заказа")
    old = DB.get(oid, {})
    rec = {**old, **s.model_dump(exclude_unset=True), "order_id": oid}
    if rec.get("gps_link") != old.get("gps_link"):
        rec.update(current_lat=None, current_lng=None, has_valid_gps=None, gps_status=None)
    DB[oid] = rec
    await save_db_async()
    if rec.get("gps_link"):
        spawn(fetch_single_order_coords(oid, rec, force=True), f"gps-{oid}")
    return {"ok": True}

@app.delete("/api/admin/shipments/{order_id}")
async def admin_delete(order_id: str, user=Depends(check_admin)):
    if order_id in DB:
        del DB[order_id]
        await save_db_async()
    return {"ok": True}

# ============================================================================
# ЗАГРУЗКА EXCEL
# ============================================================================
class UploadError(ValueError):
    pass

SHEET = "Исх.логистика"
REQUIRED_COLS = {"Заказ", "Адрес доставки", "Перевозчик", "Машина", "статус доставки"}

def _s(v):
    return None if pd.isna(v) or str(v).strip() in ("", "nan") else str(v).strip()

def _fmt_date(v):
    s = _s(v)
    if s is None: return None
    try: return pd.to_datetime(s, dayfirst=True).strftime("%Y-%m-%d")
    except Exception: return s[:10]

def _safe_float(v):
    s = _s(v)
    if s is None: return None
    try: return float(s.replace(",", "."))
    except ValueError: return None

def parse_excel(contents: bytes):
    try:
        xl = pd.ExcelFile(io.BytesIO(contents), engine="openpyxl")
    except Exception:
        raise UploadError("Не удалось открыть файл. Нужен .xlsx")
    if SHEET not in xl.sheet_names:
        raise UploadError(f"В файле нет листа '{SHEET}'")
    df = xl.parse(SHEET, dtype=str)
    missing = REQUIRED_COLS - set(df.columns)
    if missing:
        raise UploadError(f"Нет колонок: {', '.join(sorted(missing))}")
    new_db, skipped, dups = {}, 0, []
    for _, row in df.iterrows():
        oid = _s(row.get("Заказ"))
        if not oid:
            skipped += 1
            continue
        if oid in new_db:
            dups.append(oid)
        new_db[oid] = {
            "order_id": oid,
            "address": _s(row.get("Адрес доставки")),
            "weight": _safe_float(row.get("вес")),
            "plan_arrival": _fmt_date(row.get("Дата прибытия")),
            "carrier": _s(row.get("Перевозчик")),
            "vehicle": _s(row.get("Машина")),
            "driver": _s(row.get("Водитель")),
            "gps_link": _s(row.get("ссылка")),
            "comment": _s(row.get("Примечания")),
            "status": (_s(row.get("статус доставки")) or "").lower(),
            "has_valid_gps": None,
        }
    return new_db, skipped, dups

@app.post("/api/admin/upload")
async def admin_upload_excel(file: UploadFile = File(...), user=Depends(check_admin)):
    global DB
    contents = await file.read(MAX_UPLOAD + 1)
    if len(contents) > MAX_UPLOAD:
        raise HTTPException(413, "Файл больше 10 МБ")
    try:
        new_db, skipped, dups = await asyncio.to_thread(parse_excel, contents)
    except UploadError as e:
        raise HTTPException(400, str(e))
    except Exception:
        log(f"[UPLOAD] {traceback.format_exc(limit=3)}")
        raise HTTPException(500, "Ошибка разбора файла (подробности в логах)")
    for oid, o in new_db.items():
        old = DB.get(oid)
        if old and old.get("gps_link") == o.get("gps_link"):
            for k in ("current_lat", "current_lng", "has_valid_gps", "gps_updated_at", "gps_status"):
                o[k] = old.get(k)
    DB = new_db
    await save_db_async()
    spawn(update_all_coords(), "gps-after-upload")
    msg = f"Загружено {len(DB)} заказов. Пропущено пустых: {skipped}."
    if dups:
        msg += f" Дубли (взята последняя): {', '.join(dups[:10])}"
    return {"ok": True, "message": msg}

# ============================================================================
# HTML СТРАНИЦЫ (ГЛАВНАЯ И АДМИНКА) - БЕЗ ИЗМЕНЕНИЙ
# ============================================================================
@app.get("/", response_class=HTMLResponse)
async def user_portal():
    return """<!DOCTYPE html>
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
    .stale { color: #b45309; font-size: 12px; margin-top: 8px; }
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
    let currentTab = 'in_transit'; let map = null; let ORDERS = [];
    const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
    function toggleTheme() { const isDark = document.documentElement.getAttribute('data-theme') === 'dark'; document.documentElement.setAttribute('data-theme', isDark ? 'light' : 'dark'); localStorage.setItem('theme', isDark ? 'light' : 'dark'); }
    document.documentElement.setAttribute('data-theme', localStorage.getItem('theme') || 'light');
    function relTime(ts) { if (!ts) return '—'; const diff = Math.floor(Date.now()/1000) - ts; if (diff < 60) return 'только что'; if (diff < 3600) return `${Math.floor(diff/60)} мин назад`; if (diff < 86400) return `${Math.floor(diff/3600)} ч назад`; return `${Math.floor(diff/86400)} дн назад`; }
    function isStale(ts) { return ts && (Date.now()/1000 - ts) > 1800; }
    async function loadTabs() { const res = await fetch('/api/settings/tabs'); const settings = await res.json(); document.querySelectorAll('.tab').forEach(tab => { const status = tab.dataset.status; const key = 'show_' + status; if (settings[key] !== false) { tab.classList.add('visible'); } else { tab.classList.remove('visible'); } }); }
    function setTab(s) { currentTab = s; document.querySelectorAll('.tab').forEach(t => t.classList.remove('active')); const el = document.querySelector(`.tab[data-status="${s}"]`); if (el) el.classList.add('active'); loadOrders(); }
    function debounceSearch() { clearTimeout(window.searchTimeout); window.searchTimeout = setTimeout(loadOrders, 300); }
    async function loadOrders(force_refresh = false) {
        const search = document.getElementById('searchInput').value.trim();
        const sort = document.getElementById('sortSelect').value;
        document.getElementById('ordersGrid').innerHTML = '<div style="text-align:center;width:100%;">Загрузка...</div>';
        const cacheParam = force_refresh ? `&force_refresh=true&_t=${Date.now()}` : '';
        const url = `/api/shipments?status=${currentTab}&sort_by=${sort}${search ? '&search='+encodeURIComponent(search) : ''}${cacheParam}`;
        try {
            const res = await fetch(url, { cache: force_refresh ? "no-store" : "default" });
            const orders = await res.json();
            ORDERS = orders;
            const grid = document.getElementById('ordersGrid');
            if (!orders.length) { grid.innerHTML = '<div style="text-align:center;width:100%;">Нет заказов</div>'; return; }
            grid.innerHTML = orders.map((o, i) => {
                const done = ['завершено', 'выгружено'].includes(o.status);
                let badge = done ? '<span class="order-status delivered">Выгружено</span>' : (o.status === 'в пути' ? '<span class="order-status in-transit">В пути</span>' : '');
                let loc = o.comment ? `<div class="loc-info">📝 ${esc(o.comment)}</div>` : '';
                let stale = isStale(o.gps_updated_at) ? `<div class="stale">⚠️ Данные GPS устарели (${relTime(o.gps_updated_at)})</div>` : '';
                return `<div class="order-card ${done ? 'delivered' : ''}" onclick="openOrder(${i})">
                    <div style="display:flex; justify-content:space-between; margin-bottom:10px;"><span class="order-id">${esc(o.order_id)}</span>${badge}</div>
                    <div class="order-row"><strong>Адрес:</strong> ${esc(o.address) || '—'}</div>
                    <div class="order-row"><strong>Перевозчик:</strong> ${esc(o.carrier) || '—'}</div>
                    <div class="order-row"><strong>Машина:</strong> ${esc(o.vehicle) || '—'}</div>
                    <div class="order-row"><strong>Вес:</strong> ${o.weight ? esc(o.weight) + ' кг' : '—'}</div>
                    ${loc}${stale}
                </div>`;
            }).join('');
        } catch(e) { document.getElementById('ordersGrid').innerHTML = '<div style="text-align:center;width:100%;color:red;">Ошибка загрузки</div>'; }
    }
    function refreshData() { const btn = document.getElementById('refreshBtn'); btn.innerHTML = '⏳ Загрузка...'; btn.disabled = true; loadOrders(true).then(async () => { await new Promise(r => setTimeout(r, 15000)); await loadOrders(); }).finally(() => { btn.innerHTML = '🔄 Обновить'; btn.disabled = false; }); }
    async function openOrder(i) {
        const o = ORDERS[i];
        document.getElementById('modal').classList.add('open');
        const updated = o.gps_updated_at ? `🕒 GPS: ${relTime(o.gps_updated_at)}` : '';
        document.getElementById('modalContent').innerHTML = `<h2>📦 ${esc(o.order_id)}</h2>
            <div class="modal-info">
                <div><strong>Адрес</strong>${esc(o.address) || '—'}</div>
                <div><strong>Перевозчик</strong>${esc(o.carrier) || '—'}</div>
                <div><strong>Машина</strong>${esc(o.vehicle) || '—'}</div>
                <div><strong>Вес</strong>${o.weight ? esc(o.weight) + ' кг' : '—'}</div>
                <div><strong>Прибытие</strong>${esc(o.plan_arrival) || '—'}</div>
                <div><strong>GPS</strong>${updated || '—'}</div>
            </div>
            ${o.comment ? `<div style="margin-top:20px; padding:12px; background:rgba(0,0,0,0.05); border-radius:8px;"><strong>Примечание:</strong> ${esc(o.comment)}</div>` : ''}
            <div id="map"><div class="spinner"></div>Загрузка координат...</div>`;
        if (o.current_lat != null && o.current_lng != null) { renderMap(o.current_lat, o.current_lng); } else if (o.can_refresh) { loadCoords(o.order_id); } else { document.getElementById('map').innerHTML = '<div style="text-align:center;padding:50px;color:#888;">GPS отсутствует.</div>'; }
    }
    async function loadCoords(orderId) {
        const mapDiv = document.getElementById('map');
        mapDiv.innerHTML = '<div class="spinner"></div><div>Запрос к спутнику...</div>';
        try {
            const res = await fetch(`/api/coords/${encodeURIComponent(orderId)}`);
            if (res.ok) { const data = await res.json(); renderMap(data.lat, data.lng); } else { const errorData = await res.json(); mapDiv.innerHTML = `<div style="text-align:center;padding:20px;color:#888;">${esc(errorData.detail) || 'GPS недоступен'}</div>`; }
        } catch(e) { mapDiv.innerHTML = `<div style="text-align:center;padding:20px;color:#888;">Ошибка сети</div>`; }
    }
    function renderMap(lat, lng) { const mapDiv = document.getElementById('map'); mapDiv.innerHTML = ''; if (map) map.remove(); map = L.map('map').setView([lat, lng], 8); L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png').addTo(map); L.marker([lat, lng]).addTo(map); }
    function closeModal() { document.getElementById('modal').classList.remove('open'); if(map){map.remove(); map=null;} }
    loadTabs().then(() => { const first = document.querySelector('.tab.visible'); setTab(first ? first.dataset.status : 'in_transit'); });
    </script>
</body>
</html>"""

@app.get("/admin", response_class=HTMLResponse)
async def admin_panel(user=Depends(check_admin)):
    return """<!DOCTYPE html>
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
            <input type="file" id="excelFile" accept=".xlsx">
            <button class="btn btn-blue" onclick="uploadExcel()" id="uploadBtn">Обновить базу из файла</button>
            <span id="uploadStatus"></span>
        </div>
        <p style="font-size: 12px; color: #666; margin-top: 10px;">Лист <b>"Исх.логистика"</b>. Предыдущая база заменяется. Максимум 10 МБ.</p>
    </div>
    <input id="filter" placeholder="Поиск по номеру, адресу, машине..." oninput="renderTable()"/>
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
    let allData = []; let VIEW = [];
    const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
    async function loadSettings() { const res = await fetch('/api/settings/tabs'); const s = await res.json(); document.getElementById('chk_in_transit').checked = s.show_in_transit !== false; document.getElementById('chk_delivered').checked = s.show_delivered !== false; document.getElementById('chk_with_gps').checked = s.show_with_gps !== false; }
    async function saveTabs() { const payload = { show_in_transit: document.getElementById('chk_in_transit').checked, show_delivered: document.getElementById('chk_delivered').checked, show_with_gps: document.getElementById('chk_with_gps').checked }; await fetch('/api/admin/settings/tabs', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload) }); }
    async function load() { const res = await fetch('/api/admin/shipments'); allData = await res.json(); renderTable(); }
    function renderTable() {
        const q = document.getElementById('filter').value.toLowerCase();
        VIEW = allData.filter(s => { const hay = [s.order_id, s.address, s.vehicle, s.carrier].map(x => String(x || '')).join(' ').toLowerCase(); return hay.includes(q); });
        document.querySelector('#shipments tbody').innerHTML = VIEW.map((s, i) => `
            <tr>
                <td><b>${esc(s.order_id)}</b></td>
                <td>${esc(s.address) || ''}</td>
                <td>${esc(s.carrier) || ''}</td>
                <td>${esc(s.vehicle) || ''}</td>
                <td>${esc(s.status) || '—'}</td>
                <td>
                    <button class="btn btn-blue" style="padding:4px 8px;" onclick="openEdit(VIEW[${i}])">✏️</button>
                    <button class="btn btn-red" style="padding:4px 8px;" onclick="del(VIEW[${i}].order_id)">🗑</button>
                </td>
            </tr>
        `).join('');
    }
    function openAdd() { ['order_id','address','weight','carrier','vehicle','driver','gps_link','comment','plan_arrival'].forEach(f => document.getElementById('f_'+f).value = ''); document.getElementById('f_order_id').disabled = false; document.getElementById('f_status').value = ''; document.getElementById('modalTitle').innerText = 'Добавить заказ'; document.getElementById('modal').style.display = 'block'; }
    function openEdit(s) { ['order_id','address','weight','carrier','vehicle','driver','gps_link','comment','plan_arrival'].forEach(f => document.getElementById('f_'+f).value = s[f] || ''); document.getElementById('f_order_id').disabled = true; document.getElementById('f_status').value = s.status || ''; document.getElementById('modalTitle').innerText = 'Редактировать: ' + s.order_id; document.getElementById('modal').style.display = 'block'; }
    function closeModal() { document.getElementById('modal').style.display = 'none'; }
    async function saveOrder() {
        const payload = { order_id: document.getElementById('f_order_id').value, address: document.getElementById('f_address').value, weight: parseFloat(document.getElementById('f_weight').value) || null, carrier: document.getElementById('f_carrier').value, vehicle: document.getElementById('f_vehicle').value, driver: document.getElementById('f_driver').value, gps_link: document.getElementById('f_gps_link').value, comment: document.getElementById('f_comment').value, plan_arrival: document.getElementById('f_plan_arrival').value, status: document.getElementById('f_status').value };
        await fetch('/api/admin/shipments', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload) }); closeModal(); load();
    }
    async function del(order_id) { if (!confirm('Удалить ' + order_id + '?')) return; await fetch('/api/admin/shipments/' + encodeURIComponent(order_id), { method: 'DELETE' }); load(); }
    async function uploadExcel() {
        const fileInput = document.getElementById('excelFile');
        if (!fileInput.files.length) { alert("Выберите файл!"); return; }
        const formData = new FormData(); formData.append("file", fileInput.files[0]);
        const btn = document.getElementById('uploadBtn'); const status = document.getElementById('uploadStatus');
        btn.disabled = true; btn.innerText = "⏳ Обработка..."; status.innerText = "Пожалуйста, подождите..."; status.style.color = "blue";
        try {
            const res = await fetch('/api/admin/upload', { method: 'POST', body: formData });
            const data = await res.json();
            if (res.ok) { status.innerText = "✅ " + data.message + " (" + new Date().toLocaleTimeString() + ")"; status.style.color = "green"; load(); }
            else { status.innerText = "❌ Ошибка: " + (data.detail || 'неизвестно'); status.style.color = "red"; }
        } catch(e) { status.innerText = "❌ Ошибка сети"; status.style.color = "red"; }
        finally { btn.disabled = false; btn.innerText = "Обновить базу из файла"; }
    }
    loadSettings(); load();
    </script>
</body>
</html>"""
