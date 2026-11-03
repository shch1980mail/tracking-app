from fastapi import FastAPI, HTTPException, Depends, UploadFile, File
from fastapi.responses import HTMLResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from typing import Optional
from contextlib import asynccontextmanager
import os, json, re, requests, asyncio, io, datetime as dt
import pandas as pd
import openpyxl

# ============================================================
# УНИВЕРСАЛЬНЫЙ АДАПТЕР ДЛЯ MAXAPI
# ============================================================
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
    print("✅ Используется библиотека: love-apples/maxapi")
except ImportError:
    try:
        from maxapi import Bot, Dispatcher, Command, InlineKeyboardBuilder
        USE_ATTACHMENTS = False
        MAXAPI_VARIANT = "sdk"
        print("✅ Используется библиотека: maxapi-sdk")
    except ImportError:
        raise ImportError("Не найдена ни одна библиотека MAX. Установите 'maxapi[fastapi]' в requirements.txt")

from maxapi.webhook.fastapi import FastAPIMaxWebhook


def make_keyboard(callbacks: list, links: list = None):
    if MAXAPI_VARIANT == "love-apples":
        builder = InlineKeyboardBuilder()
        for text, payload in callbacks:
            builder.row(CallbackButton(text=text, payload=payload))
        if links:
            for text, url in links:
                builder.row(LinkButton(text=text, url=url))
        return builder.as_markup()
    else:
        builder = InlineKeyboardBuilder()
        for text, payload in callbacks:
            builder.callback(text, payload)
        if links:
            for text, url in links:
                builder.link(text, url)
        return builder


async def send_message(message, text: str, callbacks: list = None, links: list = None):
    if callbacks or links:
        kb = make_keyboard(callbacks or [], links)
        if USE_ATTACHMENTS:
            return await message.answer(text=text, attachments=[kb])
        return await message.answer(text=text, keyboard=kb)
    return await message.answer(text=text)


async def edit_message(message, text: str, callbacks: list = None, links: list = None):
    if callbacks or links:
        kb = make_keyboard(callbacks or [], links)
        if USE_ATTACHMENTS:
            return await message.edit(text=text, attachments=[kb])
        return await message.edit(text=text, keyboard=kb)
    return await message.edit(text=text)


# ============================================================
# НАСТРОЙКИ
# ============================================================
ADMIN_USER = os.environ.get("ADMIN_USER", "admin")
ADMIN_PASS = os.environ.get("ADMIN_PASS", "admin123")

SETTINGS_FILE = "settings.json"
DEFAULT_SETTINGS = {"show_in_transit": True, "show_delivered": True, "show_with_gps": False}

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


DB_FILE = "shipments.json"
DB: dict = {}

# === НАСТРОЙКА БОТА MAX ===
MAX_BOT_TOKEN = os.environ.get("MAX_BOT_TOKEN")
if not MAX_BOT_TOKEN:
    raise ValueError("❌ Переменная окружения MAX_BOT_TOKEN не задана! Добавьте её в Layero → Переменные окружения.")

PUBLIC_URL = os.environ.get("PUBLIC_URL", "").rstrip("/")
if not PUBLIC_URL:
    raise ValueError("❌ Переменная PUBLIC_URL не задана. Укажите публичный HTTPS-домен Layero (например, https://your-project.layero.app)")

max_bot = Bot(token=MAX_BOT_TOKEN)
max_dp = Dispatcher()
webhook = FastAPIMaxWebhook(dp=max_dp, bot=max_bot)


# ============================================================
# ХРАНИЛИЩЕ: ключи, годы, слияние
# ============================================================
STATUS_CANON = {
    "планируется": "планируется",
    "в плане": "планируется",
    "в пути": "в пути",
    "выгружено": "выгружено",
    "завершено": "завершено",
    "": "планируется",
    "nan": "планируется",
}
IN_TRANSIT = {"в пути"}
DELIVERED = {"завершено", "выгружено"}
HIDDEN_STATUSES = {"планируется"}
SERVICE_IDS = {"оборудование", "выставка", "рем.комп. для конша"}


def norm_status(s: str) -> str:
    s = (s or "").strip().lower().replace("ё", "е")
    return STATUS_CANON.get(s, s or "планируется")


def parse_iso_date(v):
    if v is None:
        return None
    if isinstance(v, (dt.date, dt.datetime)):
        return v.strftime("%Y-%m-%d")
    s = str(v).strip()
    if not s or s.lower() == "nan":
        return None
    try:
        return pd.to_datetime(s, dayfirst=False).strftime("%Y-%m-%d")
    except Exception:
        return s[:10] if re.match(r"\d{4}-\d{2}-\d{2}", s) else None


def year_of(rec: dict, fallback: int = None) -> int:
    for k in ("ship_date", "plan_arrival", "arrival_date"):
        v = rec.get(k)
        if v and re.match(r"\d{4}", str(v)):
            return int(str(v)[:4])
    if fallback:
        return fallback
    return dt.date.today().year


def make_key(order_id: str, year: int, row_index: int = None, service: bool = False) -> str:
    base = f"{order_id}__{year}"
    if service and row_index is not None:
        return f"{base}__row{row_index}"
    return base


def is_service_id(order_id: str) -> bool:
    oid = (order_id or "").strip()
    return oid.lower() in SERVICE_IDS or not re.match(r"^ТПК\d{8}$", oid)


def is_hidden_status(rec: dict) -> bool:
    return norm_status(rec.get("status")) in HIDDEN_STATUSES


def load_db():
    global DB
    if os.path.exists(DB_FILE):
        with open(DB_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
        DB = migrate_raw_db(raw)
    else:
        DB = {}
    return DB


def migrate_raw_db(raw: dict) -> dict:
    out = {}
    for old_key, rec in raw.items():
        if "_key" in rec:
            out[rec["_key"]] = rec
            continue
        year = year_of(rec)
        service = is_service_id(rec.get("order_id", old_key))
        key = make_key(rec.get("order_id", old_key), year, service=service)
        if key in out:
            i = 2
            while f"{key}#{i}" in out:
                i += 1
            key = f"{key}#{i}"
        rec["_key"] = key
        rec["year"] = year
        rec.setdefault("history", [])
        rec["status"] = norm_status(rec.get("status"))
        out[key] = rec
    return out


def save_db():
    with open(DB_FILE, "w", encoding="utf-8") as f:
        json.dump(DB, f, ensure_ascii=False, indent=2)


def merge_into_db(new_records: list) -> dict:
    global DB
    now = dt.datetime.utcnow().isoformat(timespec="seconds")
    stats = {"added": 0, "updated": 0, "unchanged": 0}
    for rec in new_records:
        key = rec["_key"]
        if key in DB:
            old = DB[key]
            rec["history"] = old.get("history", []) + [{
                "loaded_at": now,
                "snapshot": {k: old.get(k) for k in old if k not in ("history",)},
            }]
            rec["first_seen_at"] = old.get("first_seen_at", now)
            if {k: old.get(k) for k in old if k != "history"} == {k: rec.get(k) for k in rec if k != "history"}:
                stats["unchanged"] += 1
            else:
                stats["updated"] += 1
        else:
            rec["history"] = rec.get("history", [])
            rec["first_seen_at"] = now
            stats["added"] += 1
        rec["updated_at"] = now
        DB[key] = rec
    cur_year = dt.date.today().year
    for r in DB.values():
        if r.get("year", cur_year) < cur_year and norm_status(r.get("status")) in DELIVERED:
            r["archived"] = True
    save_db()
    return stats


# ============================================================
# ПАРСИНГ EXCEL
# ============================================================
COLUMN_MAP = {
    "Заказ": "order_id",
    "Направление": "direction",
    "Адрес доставки": "address",
    "вес": "weight",
    "Дата отгрузки (ФАКТ)": "ship_date",
    "Дата прибытия": "plan_arrival",
    "Перевозчик": "carrier",
    "Машина": "vehicle",
    "Водитель": "driver",
    "Ставка": "rate",
    "Срок транзита": "transit_days",
    "Тип тс": "vehicle_type",
    "Термологер": "thermologger",
    "Примечания": "comment",
    "ссылка": "gps_link",
    "статус доставки": "status",
    "местоположение": "location_note",
}

REQUIRED_COLS = ["Заказ"]


def strip_filters(wb: openpyxl.Workbook):
    for ws in wb.worksheets:
        try:
            ws.auto_filter.ref = None
            ws.sheet_state = "visible"
        except Exception:
            pass
        for rd in ws.row_dimensions.values():
            rd.hidden = False
            rd.outlineLevel = 0
            rd.collapsed = False
        for cd in ws.column_dimensions.values():
            cd.hidden = False
            cd.outlineLevel = 0
            cd.collapsed = False
        try:
            if ws.sheet_properties and ws.sheet_properties.outlinePr:
                ws.sheet_properties.outlinePr.summaryBelow = False
                ws.sheet_properties.outlinePr.summaryRight = False
        except Exception:
            pass


def _safe_float(v):
    if v is None:
        return None
    s = str(v).strip().replace(" ", "").replace(",", ".")
    if not s or s.lower() == "nan":
        return None
    try:
        return float(s)
    except Exception:
        return None


def _safe_str(v):
    if v is None:
        return None
    s = str(v).strip()
    if not s or s.lower() == "nan":
        return None
    return s


def parse_excel_to_records(contents: bytes, fallback_year: int = None) -> tuple[list, dict]:
    wb = openpyxl.load_workbook(io.BytesIO(contents))
    if "Исх.логистика" not in wb.sheetnames:
        raise HTTPException(400, f"В файле нет листа 'Исх.логистика'. Листы: {wb.sheetnames}")

    strip_filters(wb)
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)

    df = pd.read_excel(buf, sheet_name="Исх.логистика", header=0, dtype=str, keep_default_na=False)
    df.columns = [str(c).strip() for c in df.columns]

    missing = [c for c in REQUIRED_COLS if c not in df.columns]
    if missing:
        raise HTTPException(400, f"Не найдены колонки: {missing}. Есть: {list(df.columns)}")

    records = []
    audit = {
        "missing_in_db": [], "stale_in_db": [], "duplicates": {}, "misaligned": [],
        "nonstandard_ids": [], "skipped_empty": 0, "weight_parse_failed": [],
        "date_parse_failed": [], "gps_looks_like_text": [],
    }

    for idx, row in df.iterrows():
        raw_order = _safe_str(row.get("Заказ"))
        if not raw_order:
            audit["skipped_empty"] += 1
            continue
        order_id = raw_order
        service = is_service_id(order_id)
        if service:
            audit["nonstandard_ids"].append(order_id)

        rec = {"order_id": order_id, "is_service_row": service, "row_index": int(idx) + 2}
        warnings = []

        for excel_col, field in COLUMN_MAP.items():
            if excel_col == "Заказ":
                continue
            if excel_col not in df.columns:
                rec[field] = None
                continue
            val = row.get(excel_col)
            if field == "weight":
                fv = _safe_float(val)
                rec[field] = fv
                if fv is None and _safe_str(val):
                    warnings.append("weight_parse_failed")
                    audit["weight_parse_failed"].append({"order_id": order_id, "row": rec["row_index"], "raw": str(val)})
            elif field in ("ship_date", "plan_arrival"):
                dv = parse_iso_date(val)
                rec[field] = dv
                if dv is None and _safe_str(val):
                    warnings.append(f"{field}_parse_failed")
                    audit["date_parse_failed"].append({"order_id": order_id, "row": rec["row_index"], "field": field, "raw": str(val)})
            elif field == "status":
                rec["status"] = norm_status(_safe_str(val) or "")
            elif field == "rate":
                rec[field] = _safe_float(val)
            elif field == "gps_link":
                s = _safe_str(val)
                rec[field] = s
                if s and not s.startswith("http"):
                    audit["gps_looks_like_text"].append({"order_id": order_id, "row": rec["row_index"], "raw": s})
            else:
                rec[field] = _safe_str(val)

        arrival_fact = None
        if norm_status(rec.get("status")) in DELIVERED:
            if rec.get("plan_arrival"):
                arrival_fact = rec["plan_arrival"]
            else:
                blob = " ".join(str(rec.get(k) or "") for k in ("comment", "location_note"))
                m = re.search(r"\b(\d{2})[./](\d{2})[./](\d{2,4})\b", blob)
                if m:
                    d, mo, y = m.group(1), m.group(2), m.group(3)
                    if len(y) == 2:
                        y = "20" + y
                    arrival_fact = f"{y}-{mo}-{d}"
        rec["arrival_date"] = arrival_fact
        rec["arrival_fact_missing"] = bool(norm_status(rec.get("status")) in DELIVERED and not arrival_fact)

        rec["year"] = year_of(rec, fallback=fallback_year)
        rec["_key"] = make_key(order_id, rec["year"], row_index=rec["row_index"], service=service)
        rec["data_warnings"] = warnings
        rec["archived"] = False
        
        rec["has_valid_gps"] = None
        rec["gps_checked"] = False
        rec["current_lat"] = None
        rec["current_lng"] = None
        
        records.append(rec)

    keys = [r["_key"] for r in records]
    for k in set(keys):
        if keys.count(k) > 1:
            audit["duplicates"][k] = keys.count(k)

    seen = set()
    for r in records:
        if r["_key"] in seen:
            r["_key"] = f"{r['_key']}__row{r['row_index']}"
        seen.add(r["_key"])

    return records, audit


# ============================================================
# LIFESPAN / APP
# ============================================================
@asynccontextmanager
async def lifespan(app: FastAPI):
    global DB
    load_db()
    if DB:
        asyncio.create_task(update_all_coords())
    asyncio.create_task(_register_webhook_once())
    print("✅ MAX-бот запущен (Webhook mode)")
    async with webhook.lifespan(app):
        yield
    print("🛑 Приложение остановлено")


app = FastAPI(title="ТПК Отслеживание", lifespan=lifespan)
security = HTTPBasic()
app.mount("/static", StaticFiles(directory="static"), name="static")
webhook.setup(app, path="/webhook")


async def _register_webhook_once():
    try:
        await max_bot.delete_webhook()
    except Exception:
        pass
    try:
        await max_bot.subscribe_webhook(
            url=f"{PUBLIC_URL}/webhook",
            update_types=["message_created", "message_callback", "bot_started"],
        )
        print(f"✅ Вебхук зарегистрирован: {PUBLIC_URL}/webhook")
    except Exception as e:
        print(f"⚠️ Ошибка регистрации вебхука: {e}")
        print("   Проверьте PUBLIC_URL и доступность HTTPS.")


class Shipment(BaseModel):
    order_id: str
    direction: Optional[str] = None
    address: Optional[str] = None
    weight: Optional[float] = None
    ship_date: Optional[str] = None
    plan_arrival: Optional[str] = None
    arrival_date: Optional[str] = None
    carrier: Optional[str] = None
    vehicle: Optional[str] = None
    driver: Optional[str] = None
    gps_link: Optional[str] = None
    comment: Optional[str] = None
    status: Optional[str] = None


# ============================================================
# GPS
# ============================================================
def get_coords_from_baltgps(gps_link: str):
    if not gps_link or "maps.baltgps.ru" not in gps_link:
        return None, None, "no_link"
    try:
        token = gps_link.split("?t=")[-1]
        r = requests.get("https://maps.baltgps.ru/wialon/ajax.html", params={
            "svc": "token/login", "params": json.dumps({"token": token})
        }, timeout=(3.0, 5.0))
        data = r.json()
        if "error" in data or "token" not in data:
            return None, None, "invalid_token"
        items = json.loads(data["token"]).get("items", [])
        if not items:
            return None, None, "no_units"
        unit_id = items[0]
        sid = data.get("eid")
        r2 = requests.get("https://maps.baltgps.ru/wialon/ajax.html", params={
            "svc": "unit/calc_last", "params": json.dumps([unit_id]), "sid": sid
        }, timeout=(3.0, 5.0))
        pos_data = r2.json()
        if isinstance(pos_data, list) and pos_data:
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


async def fetch_single_order_coords(rec: dict):
    loop = asyncio.get_event_loop()
    lat, lng, status = await loop.run_in_executor(None, get_coords_from_baltgps, rec["gps_link"])
    if lat and lng:
        rec["current_lat"] = lat
        rec["current_lng"] = lng
        rec["has_valid_gps"] = True
    else:
        rec["current_lat"] = None
        rec["current_lng"] = None
        if status != "error":
            rec["has_valid_gps"] = False
    rec["gps_checked"] = True


async def update_all_coords():
    for rec in DB.values():
        if rec.get("has_valid_gps") is False:
            rec["current_lat"] = None
            rec["current_lng"] = None
    tasks = [fetch_single_order_coords(r) for r in DB.values() if r.get("gps_link") and "baltgps" in r["gps_link"] and not r.get("gps_checked")]
    sem = asyncio.Semaphore(10)
    async def _limited(coro):
        async with sem:
            await coro
    await asyncio.gather(*(_limited(t) for t in tasks))
    save_db()


# ============================================================
# БОТ
# ============================================================
def get_main_menu():
    return [
        ("📋 Выбрать заказ", "menu_choose_order"),
        ("🔄 Обновить GPS", "menu_update_gps"),
        ("❓ Помощь", "menu_help"),
    ]


def get_orders_menu():
    items = []
    for r in DB.values():
        if norm_status(r.get("status")) == "в пути":
            mark = "🟢" if r.get("has_valid_gps") else "🔴"
            items.append((f"{mark} {r['order_id']}", f"order_{r['_key']}"))
            if len(items) >= 10:
                break
    items.append(("🔙 Назад в меню", "menu_back"))
    return items


def find_by_short(q: str):
    q = q.upper()
    if q.isdigit() and len(q) == 4:
        for r in DB.values():
            if is_hidden_status(r):
                continue
            if r["order_id"].endswith(q):
                return r
    return None


def find_all_by_order_id(order_id: str):
    return [r for r in DB.values() if r["order_id"] == order_id and not is_hidden_status(r)]


def format_order(r: dict) -> str:
    s = (
        f"📦 **Заказ:** {r['order_id']}\n"
        f"📍 **Адрес:** {r.get('address') or '—'}\n"
        f"🚚 **Перевозчик:** {r.get('carrier') or '—'}\n"
        f"🚛 **Машина:** {r.get('vehicle') or '—'}\n"
        f"📊 **Статус:** {norm_status(r.get('status')) or '—'}\n"
    )
    if norm_status(r.get("status")) in DELIVERED:
        s += f"📅 **Дата отправки:** {r.get('ship_date') or '—'}\n"
        s += f"📅 **Прибытие (план):** {r.get('plan_arrival') or '—'}\n"
        s += f"📅 **Дата выгрузки:** {r.get('arrival_date') or 'нет данных'}\n"
    if r.get("comment"):
        s += f"📝 **Примечание:** {r['comment']}\n"
    return s


@max_dp.message_created(Command("start"))
async def handle_start(event):
    await send_message(event.message, "👋 Добро пожаловать в бот ТПК! Выберите действие:", get_main_menu())


@max_dp.message_created()
async def handle_message(event):
    if not event.message or not event.message.body or not event.message.body.text:
        return
    text = event.message.body.text.strip()
    if text.startswith("/"):
        return
    found = find_by_short(text) or (find_all_by_order_id(text) or [None])[0]
    if found:
        links = []
        if found.get("current_lat") and found.get("current_lng"):
            links.append(("🗺️ Открыть карту", f"https://yandex.ru/maps/?pt={found['current_lng']},{found['current_lat']}&z=12"))
        elif found.get("gps_link"):
            links.append(("🗺️ Открыть карту BaltGPS", found["gps_link"]))
        await send_message(event.message, format_order(found), [("🔙 Назад в меню", "menu_back")], links)
    else:
        await event.message.answer(f"❌ Заказ по запросу «{text}» не найден. Проверьте номер или выберите действие из меню.")


@max_dp.message_callback()
async def handle_callback(event):
    if not event.callback or not event.callback.payload:
        return
    payload = event.callback.payload
    await event.answer()
    if payload == "menu_choose_order":
        await edit_message(event.message, "Выберите заказ из списка:", get_orders_menu())
    elif payload == "menu_update_gps":
        await edit_message(event.message, "⏳ Запускаю обновление GPS в фоне. Это может занять минуту...", get_main_menu())
        for r in DB.values():
            r["gps_checked"] = False
        asyncio.create_task(update_all_coords())
    elif payload == "menu_help":
        help_text = (
            "👋 **Помощь**\n\n"
            "🔍 **Как искать:**\n"
            "• Введите **4 цифры** номера заказа (например, `1668`).\n"
            "• Или нажмите **📋 Выбрать заказ**.\n\n"
            "🗺️ **Что я умею:**\n"
            "• Показывать адрес, перевозчика, машину и статус.\n"
            "• Отправлять точку на карте (если есть GPS).\n"
            "• Показывать даты отправки/выгрузки для завершённых."
        )
        await edit_message(event.message, help_text, get_main_menu())
    elif payload == "menu_back":
        await edit_message(event.message, "👋 Главное меню:", get_main_menu())
    elif payload.startswith("order_"):
        key = payload[len("order_"):]
        if key in DB and not is_hidden_status(DB[key]):
            r = DB[key]
            links = []
            if r.get("current_lat") and r.get("current_lng"):
                links.append(("🗺️ Открыть карту", f"https://yandex.ru/maps/?pt={r['current_lng']},{r['current_lat']}&z=12"))
            elif r.get("gps_link"):
                links.append(("🗺️ Открыть карту BaltGPS", r["gps_link"]))
            await edit_message(event.message, format_order(r), [("🔙 Назад в меню", "menu_back")], links)
        else:
            await event.message.answer("Заказ не найден.")


# ============================================================
# АДМИН
# ============================================================
def check_admin(credentials: HTTPBasicCredentials = Depends(security)):
    if credentials.username != ADMIN_USER or credentials.password != ADMIN_PASS:
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


# ============================================================
# API ДЛЯ САЙТА
# ============================================================
def public_view(r: dict) -> dict:
    return {
        "order_id": r["order_id"],
        "_key": r["_key"],
        "year": r.get("year"),
        "direction": r.get("direction"),
        "address": r.get("address"),
        "weight": r.get("weight"),
        "ship_date": r.get("ship_date"),
        "plan_arrival": r.get("plan_arrival"),
        "arrival_date": r.get("arrival_date"),
        "arrival_fact_missing": r.get("arrival_fact_missing", False),
        "carrier": r.get("carrier"),
        "vehicle": r.get("vehicle"),
        "driver": r.get("driver"),
        "gps_link": r.get("gps_link"),
        "comment": r.get("comment"),
        "location_note": r.get("location_note"),
        "status": norm_status(r.get("status")),
        "current_lat": r.get("current_lat"),
        "current_lng": r.get("current_lng"),
        "has_valid_gps": r.get("has_valid_gps"),
        "gps_checked": r.get("gps_checked", False),
        "archived": r.get("archived", False),
        "is_service_row": r.get("is_service_row", False),
    }


@app.get("/api/years")
async def get_years():
    years = sorted({r.get("year") for r in DB.values() if r.get("year")}, reverse=True)
    return years


@app.get("/api/shipments")
async def list_shipments(
    status: Optional[str] = None,
    search: Optional[str] = None,
    sort_by: str = "gps",
    year: Optional[str] = None,
    force_refresh: bool = False,
):
    if force_refresh:
        for r in DB.values():
            r["gps_checked"] = False
        asyncio.create_task(update_all_coords())

    cur_year = dt.date.today().year
    selected_year = None
    if year and year != "all":
        try:
            selected_year = int(year)
        except ValueError:
            selected_year = cur_year
    elif year is None:
        selected_year = cur_year

    result = []
    for r in DB.values():
        if r.get("is_service_row") and not search:
            continue
        if r.get("archived") and selected_year != r.get("year") and year != "all":
            continue
        if selected_year is not None and r.get("year") != selected_year:
            continue
        s = norm_status(r.get("status"))
        if s in HIDDEN_STATUSES:
            continue
        if status == "in_transit" and s not in IN_TRANSIT:
            continue
        if status == "delivered" and s not in DELIVERED:
            continue
        if status == "with_gps" and not r.get("current_lat"):
            continue
        if search:
            q = search.lower()
            hay = " ".join(str(r.get(k) or "") for k in ("order_id", "address", "vehicle", "carrier")).lower()
            if q not in hay:
                continue
        result.append(public_view(r))

    def sort_key(x):
        is_valid = 1 if x.get("current_lat") else 0
        oid = str(x.get("order_id") or "")
        city = str(x.get("address") or "").lower()
        if sort_by == "gps":
            return (not is_valid, oid)
        if sort_by == "order_id":
            return oid
        if sort_by == "city":
            return (city == "", city, oid)
        if sort_by == "arrival_date":
            return (x.get("arrival_date") or "", oid)
        return (not is_valid, oid)

    result.sort(key=sort_key)
    return result


@app.get("/api/shipments/{order_id}")
async def get_shipments_by_order(order_id: str):
    items = find_all_by_order_id(order_id)
    if not items:
        raise HTTPException(404, "Заказ не найден")
    items.sort(key=lambda r: (r.get("year") or 0, r.get("ship_date") or ""))
    return [public_view(r) for r in items]


@app.get("/api/coords/{key}")
async def get_online_coords(key: str):
    if key not in DB:
        raise HTTPException(404, "Заказ не найден")
    r = DB[key]
    if r.get("gps_link"):
        lat, lng, status = get_coords_from_baltgps(r["gps_link"])
        if lat and lng:
            r["current_lat"], r["current_lng"], r["has_valid_gps"] = lat, lng, True
            r["gps_checked"] = True
            save_db()
            return {"lat": lat, "lng": lng}
        if status == "invalid_token":
            r["has_valid_gps"] = False
            r["gps_checked"] = True
            save_db()
            raise HTTPException(400, "Ссылка на GPS устарела")
        if status == "error":
            raise HTTPException(503, "Спутник не отвечает")
            
    if r.get("current_lat"):
        return {"lat": r["current_lat"], "lng": r["current_lng"]}
    raise HTTPException(404, "GPS недоступен")


@app.get("/api/check_gps/{key}")
async def check_gps_link(key: str):
    if key not in DB:
        raise HTTPException(404, "Заказ не найден")
    r = DB[key]
    if not r.get("gps_link"):
        return {"valid": False}
        
    if r.get("gps_checked"):
        return {"valid": r.get("has_valid_gps")}
        
    loop = asyncio.get_event_loop()
    lat, lng, status = await loop.run_in_executor(None, get_coords_from_baltgps, r["gps_link"])
    
    if lat and lng:
        r["current_lat"], r["current_lng"], r["has_valid_gps"] = lat, lng, True
        r["gps_checked"] = True
        save_db()
        return {"valid": True}
    elif status == "invalid_token" or status == "no_units":
        r["has_valid_gps"] = False
        r["gps_checked"] = True
        save_db()
        return {"valid": False}
    else:
        return {"valid": None}


@app.get("/api/admin/shipments")
async def admin_list(user=Depends(check_admin)):
    return [public_view(r) for r in DB.values()]


@app.post("/api/admin/shipments")
async def admin_create(s: Shipment, user=Depends(check_admin)):
    rec = s.model_dump()
    rec["status"] = norm_status(rec.get("status"))
    rec["year"] = year_of(rec, fallback=dt.date.today().year)
    service = is_service_id(rec["order_id"])
    rec["is_service_row"] = service
    rec["_key"] = make_key(rec["order_id"], rec["year"], service=service)
    if rec["_key"] in DB:
        rec["_key"] = f"{rec['_key']}#{int(dt.datetime.utcnow().timestamp())}"
    rec["has_valid_gps"] = None
    rec["gps_checked"] = False
    merge_into_db([rec])
    asyncio.create_task(update_all_coords())
    return {"ok": True, "_key": rec["_key"]}


@app.delete("/api/admin/shipments/{key}")
async def admin_delete(key: str, user=Depends(check_admin)):
    if key in DB:
        del DB[key]
        save_db()
    return {"ok": True}


@app.post("/api/admin/upload")
async def admin_upload_excel(file: UploadFile = File(...), user=Depends(check_admin)):
    try:
        contents = await file.read()
        m = re.search(r"(20\d{2})", file.filename or "")
        fallback_year = int(m.group(1)) if m else None
        records, audit = parse_excel_to_records(contents, fallback_year=fallback_year)
        stats = merge_into_db(records)
        asyncio.create_task(update_all_coords())

        ts = dt.datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        audit_path = f"upload_audit_{ts}.json"
        new_keys = {r["_key"] for r in records}
        stale = [r["_key"] for k, r in DB.items() if k not in new_keys and not r.get("archived")]
        audit["stale_in_db"] = stale
        with open(audit_path, "w", encoding="utf-8") as f:
            json.dump(audit, f, ensure_ascii=False, indent=2)

        last = [r["order_id"] for r in records[-5:]]
        return {
            "ok": True,
            "message": (
                f"Загружено {len(records)} строк (добавлено {stats['added']}, обновлено {stats['updated']}, "
                f"без изменений {stats['unchanged']}). Пропущено пустых: {audit['skipped_empty']}. "
                f"Дублей в файле: {len(audit['duplicates'])}. Осиротевших в БД: {len(stale)}. "
                f"Последние: {', '.join(last)}. Аудит: {audit_path}"
            ),
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"Ошибка чтения файла: {str(e)}")


# ============================================================
# ГЛАВНАЯ / АДМИНКА
# ============================================================
@app.get("/", response_class=HTMLResponse)
async def user_portal():
    return INDEX_HTML


@app.get("/admin", response_class=HTMLResponse)
async def admin_panel(user=Depends(check_admin)):
    return ADMIN_HTML


INDEX_HTML = r"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>ТПК — Отслеживание</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<style>
    :root { --bg:#f0f2f5; --card:#fff; --text:#1a1a1a; --text-sec:#6c757d; --border:#e0e0e0; --primary:#007bff; --success:#28a745; --shadow:0 2px 12px rgba(0,0,0,.08); }
    [data-theme="dark"] { --bg:#1a1a1a; --card:#2d2d2d; --text:#e8e8e8; --text-sec:#a0a0a0; --border:#404040; --primary:#4a9eff; --success:#28a745; }
    * { margin:0; padding:0; box-sizing:border-box; }
    body { font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif; background:var(--bg); color:var(--text); padding:24px; }
    .header { max-width:1100px; margin:0 auto 24px; display:flex; justify-content:space-between; align-items:center; }
    .header h1 { font-size:28px; }
    .controls { max-width:1100px; margin:0 auto 16px; display:flex; gap:10px; flex-wrap:wrap; }
    .search-box { position:relative; flex:1; min-width:250px; }
    .search-box input { width:100%; padding:14px 20px 14px 48px; border-radius:12px; border:2px solid var(--border); background:var(--card); color:var(--text); }
    .search-box::before { content:'🔍'; position:absolute; left:16px; top:50%; transform:translateY(-50%); }
    .sort-select { padding:14px; border-radius:12px; border:2px solid var(--border); background:var(--card); color:var(--text); cursor:pointer; }
    .refresh-btn { padding:14px; border-radius:12px; border:none; background:var(--primary); color:#fff; font-weight:bold; cursor:pointer; display:flex; align-items:center; gap:6px; }
    .tabs { max-width:1100px; margin:0 auto 12px; display:flex; gap:8px; background:var(--card); padding:8px; border-radius:12px; box-shadow:var(--shadow); }
    .tab { flex:1; padding:12px; border:none; background:transparent; color:var(--text-sec); font-weight:600; cursor:pointer; border-radius:8px; display:none; }
    .tab.visible { display:block; }
    .tab.active { background:var(--primary); color:#fff; }
    .years { max-width:1100px; margin:0 auto 16px; display:flex; gap:6px; flex-wrap:wrap; }
    .year-chip { padding:8px 14px; border-radius:20px; border:2px solid var(--border); background:var(--card); color:var(--text-sec); cursor:pointer; font-weight:600; }
    .year-chip.active { background:var(--primary); color:#fff; border-color:var(--primary); }
    .container { max-width:1100px; margin:0 auto; }
    .orders-grid { display:grid; grid-template-columns:repeat(auto-fill,minmax(320px,1fr)); gap:16px; }
    
    /* Карточки */
    .order-card { background:var(--card); border-radius:12px; padding:20px; box-shadow:var(--shadow); cursor:pointer; border-left:4px solid var(--primary); transition:.2s; color:var(--text); position:relative; }
    .order-card:hover { transform:translateY(-2px); }
    .order-card.delivered { border-left-color:var(--success); padding:14px 16px; }
    .order-card.delivered .order-row { margin:3px 0; }
    .order-card.delivered .order-id { font-size:15px; }
    .order-card.delivered .loc-info { display:none; }
    .order-card.delivered .order-row strong { min-width:96px; }
    
    .order-id { color:var(--primary); font-weight:700; font-size:16px; }
    .order-row { font-size:13px; margin:6px 0; color:var(--text-sec); }
    .order-row strong { color:var(--text); min-width:90px; display:inline-block; }
    .loc-info { margin-top:10px; padding:8px; background:rgba(0,123,255,.08); border-radius:6px; font-size:13px; color:var(--primary); }
    
    /* Бейджи статусов */
    .order-status { padding:4px 10px; border-radius:12px; font-size:11px; font-weight:700; }
    .in-transit { background:#d4edda; color:#155724; }
    .status-badge-delivered { background:#d1ecf1; color:#0c5460; }
    
    #modal { display:none; position:fixed; inset:0; background:rgba(0,0,0,.6); z-index:1000; padding:20px; overflow-y:auto; }
    #modal.open { display:block; }
    .modal-content { background:var(--card); max-width:900px; margin:40px auto; border-radius:16px; padding:28px; position:relative; }
    .modal-close { position:absolute; top:16px; right:16px; font-size:24px; cursor:pointer; border:none; background:none; color:var(--text); }
    #map { height:400px; margin-top:20px; border-radius:12px; display:flex; flex-direction:column; align-items:center; justify-content:center; background:#eaeaea; color:#888; gap:10px; }
    [data-theme="dark"] #map { background:#1a1a1a; }
    .modal-info { display:grid; grid-template-columns:1fr 1fr; gap:12px; margin-top:20px; }
    .modal-info strong { display:block; color:var(--text-sec); font-size:12px; text-transform:uppercase; }
    
    /* Кастомный логотип в качестве спиннера загрузки карты */
    .custom-spinner { 
        width: 50px; 
        height: 50px; 
        margin-bottom: 15px;
        animation: spin 2s linear infinite; 
    }
    @keyframes spin { 
        0% { transform: rotate(0deg); } 
        100% { transform: rotate(360deg); } 
    }
    
    .btn-reload { padding:10px 20px; background:var(--primary); color:#fff; border:none; border-radius:8px; cursor:pointer; font-weight:bold; text-decoration:none; display:inline-block; }
</style>
</head>
<body>
<!-- Обновленная шапка с логотипом -->
<div class="header">
    <div style="display:flex; align-items:center; gap:10px;">
        <img src="/static/logo-2.png" alt="Логотип" style="height:36px; width:auto;">
        <h1 style="margin:0;">ТПК — Отслеживание</h1>
    </div>
    <button onclick="toggleTheme()" style="background:none;border:none;font-size:24px;cursor:pointer;">🌙</button>
</div>
<div class="controls">
    <div class="search-box"><input id="searchInput" placeholder="Поиск по номеру, городу или машине..." oninput="debounceSearch()"></div>
    <select class="sort-select" id="sortSelect" onchange="loadOrders()">
        <option value="gps">📍 Сначала с GPS</option>
        <option value="order_id">🔢 По номеру заказа</option>
        <option value="city">🏙️ По городу</option>
        <option value="arrival_date">📅 По дате прибытия</option>
    </select>
    <button class="refresh-btn" onclick="refreshData()" id="refreshBtn">🔄 Обновить</button>
</div>
<div class="tabs" id="tabsContainer">
    <button class="tab" data-status="in_transit" onclick="setTab('in_transit')">🚚 В пути</button>
    <button class="tab" data-status="delivered" onclick="setTab('delivered')">✅ Прибывшие</button>
    <button class="tab" data-status="with_gps" onclick="setTab('with_gps')">📍 С GPS</button>
</div>
<div class="years" id="yearsContainer" style="display:none;"></div>
<div class="container"><div class="orders-grid" id="ordersGrid"></div></div>
<div id="modal"><div class="modal-content"><button class="modal-close" onclick="closeModal()">×</button><div id="modalContent"></div></div></div>
<script>
let currentTab='in_transit', currentYear=null, map=null;
function toggleTheme(){const d=document.documentElement.getAttribute('data-theme')==='dark';document.documentElement.setAttribute('data-theme',d?'light':'dark');localStorage.setItem('theme',d?'light':'dark');}
document.documentElement.setAttribute('data-theme',localStorage.getItem('theme')||'dark');

async function loadTabs(){
    const r=await fetch('/api/settings/tabs');
    const s=await r.json();
    document.querySelectorAll('.tab').forEach(t=>{
        const st=t.dataset.status;
        t.classList.toggle('visible', s['show_'+st]!==false);
    });
    const firstVisible = document.querySelector('.tab.visible');
    if (firstVisible) setTab(firstVisible.dataset.status);
}

function setTab(s){
    currentTab=s;
    document.querySelectorAll('.tab').forEach(t=>t.classList.remove('active'));
    document.querySelector(`.tab[data-status="${s}"]`).classList.add('active');
    
    const yearsBox = document.getElementById('yearsContainer');
    if (s === 'delivered') {
        yearsBox.style.display = 'flex';
        if (!currentYear) currentYear = new Date().getFullYear();
    } else {
        yearsBox.style.display = 'none';
        currentYear = new Date().getFullYear();
    }
    loadOrders();
}

function setYear(y){
    currentYear=y;
    document.querySelectorAll('.year-chip').forEach(c=>c.classList.toggle('active',c.dataset.year===String(y)));
    loadOrders();
}

function debounceSearch(){
    clearTimeout(window.t);
    window.t=setTimeout(loadOrders,300);
}

async function loadYears(){
    const r=await fetch('/api/years');
    const years=await r.json();
    const box=document.getElementById('yearsContainer');
    const cur=new Date().getFullYear();
    const list=[...new Set([cur,...years])].sort((a,b)=>b-a);
    if(!currentYear)currentYear=cur;
    box.innerHTML=list.map(y=>`<button class="year-chip ${y===currentYear?'active':''}" data-year="${y}" onclick="setYear(${y})">${y}</button>`).join('');
}

// ФУНКЦИЯ РЕНДЕРА ОДНОЙ КАРТОЧКИ
function renderCard(o) {
    const st=(o.status||'').toLowerCase();
    const isDelivered=(st==='завершено'||st==='выгружено');
    let badge='';
    if(st==='в пути') badge='<span class="order-status in-transit">В пути</span>';
    else if(st==='выгружено') badge='<span class="order-status status-badge-delivered">Выгружено</span>';
    else if(st==='завершено') badge='<span class="order-status status-badge-delivered">Завершено</span>';
    
    // ЛОГИКА ЗНАЧКА GPS
    let gpsIcon = '';
    if (o.gps_link && o.gps_link.startsWith('http') && o.has_valid_gps !== false) {
        if (o.has_valid_gps === true || (o.current_lat && o.current_lng)) {
            gpsIcon = `<span title="GPS доступен" style="margin-left:8px;font-size:16px;cursor:pointer;" onclick="event.stopPropagation(); openOrder(${JSON.stringify(o).replace(/'/g,"&#39;")})">🌍</span>`;
        } else {
            gpsIcon = `<span class="gps-loading" data-key="${o._key}" title="Проверка GPS..." style="margin-left:8px;font-size:14px;cursor:pointer;" onclick="event.stopPropagation(); openOrder(${JSON.stringify(o).replace(/'/g,"&#39;")})">⏳</span>`;
        }
    }

    const head = `<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px;">
        <span class="order-id">${o.order_id} <small style="color:var(--text-sec)">(${o.year})</small></span>
        <div style="display:flex;align-items:center;">
            ${badge}
            ${gpsIcon}
        </div>
    </div>`;
    
    if(isDelivered){
        return `<div class="order-card delivered" onclick='openOrder(${JSON.stringify(o).replace(/'/g,"&#39;")})'>
        ${head}
        <div class="order-row"><strong>Адрес:</strong> ${o.address||'—'}</div>
        <div class="order-row"><strong>Перевозчик:</strong> ${o.carrier||'—'}</div>
        <div class="order-row"><strong>ТС:</strong> ${o.vehicle||'—'}</div>
        <div style="margin-top: 10px; padding-top: 10px; border-top: 1px dashed var(--border);">
            <div class="order-row"><strong>Дата отправки:</strong> ${o.ship_date||'—'}</div>
            <div class="order-row"><strong>Дата выгрузки:</strong> ${o.arrival_date||o.plan_arrival||'—'}</div>
        </div>
        </div>`;
    }
    
    const loc=o.comment?`<div class="loc-info">📝 ${o.comment}</div>`:'';
    return `<div class="order-card" onclick='openOrder(${JSON.stringify(o).replace(/'/g,"&#39;")})'>
    ${head}
    <div class="order-row"><strong>Адрес:</strong> ${o.address||'—'}</div>
    <div class="order-row"><strong>Перевозчик:</strong> ${o.carrier||'—'}</div>
    <div class="order-row"><strong>ТС:</strong> ${o.vehicle||'—'}</div>
    <div class="order-row"><strong>Вес:</strong> ${o.weight?o.weight+' кг':'—'}</div>
    <div class="order-row"><strong>Дата отправки:</strong> ${o.ship_date||'—'}</div>
    <div class="order-row"><strong>Прибытие (план):</strong> ${o.plan_arrival||'—'}</div>
    ${loc}
    </div>`;
}

async function loadOrders(force=false){
    const s=document.getElementById('searchInput').value.trim();
    const sort=document.getElementById('sortSelect').value;
    const grid=document.getElementById('ordersGrid');
    grid.innerHTML='<div style="text-align:center;width:100%;">Загрузка...</div>';
    const cp=force?`&force_refresh=true&_t=${Date.now()}`:'';
    const url=`/api/shipments?status=${currentTab}&sort_by=${sort}&year=${currentYear}${s?'&search='+encodeURIComponent(s):''}${cp}`;
    
    try{
        const r=await fetch(url,{cache:force?'no-store':'default'});
        let orders=await r.json();
        
        if(!orders.length){
            grid.innerHTML='<div style="text-align:center;width:100%;">Нет заказов</div>';
            return;
        }

        // РАЗДЕЛЯЕМ НА ДВЕ ГРУППЫ
        const withLink = orders.filter(o => o.gps_link && o.gps_link.startsWith('http'));
        const withoutLink = orders.filter(o => !o.gps_link || !o.gps_link.startsWith('http'));

        let html = '';

        // Группа 1: С треккингом
        if (withLink.length > 0) {
            html += `<div style="grid-column: 1 / -1; margin-top: 10px; margin-bottom: 10px; display: flex; align-items: center; gap: 12px;">
                <div style="background: var(--primary); color: white; padding: 6px 16px; border-radius: 20px; font-weight: 700; font-size: 14px; box-shadow: 0 2px 8px rgba(0,123,255,0.3);">
                    🗺️ С треккингом (${withLink.length})
                </div>
                <div style="flex: 1; height: 2px; background: linear-gradient(90deg, var(--primary), transparent); border-radius: 2px;"></div>
            </div>`;
            html += withLink.map(o => renderCard(o)).join('');
        }

        // Группа 2: Без треккинга
        if (withoutLink.length > 0) {
            // Красивый разделитель
            if (withLink.length > 0) {
                html += `<div style="grid-column: 1 / -1; margin: 30px 0 20px 0; display: flex; align-items: center; gap: 10px;">
                    <div style="flex: 1; border-top: 2px dashed var(--border);"></div>
                    <div style="color: var(--text-sec); font-size: 20px;">📦</div>
                    <div style="flex: 1; border-top: 2px dashed var(--border);"></div>
                </div>`;
            }
            html += `<div style="grid-column: 1 / -1; margin-bottom: 10px; display: flex; align-items: center; gap: 12px;">
                <div style="background: var(--text-sec); color: var(--card); padding: 6px 16px; border-radius: 20px; font-weight: 700; font-size: 14px; box-shadow: 0 2px 8px rgba(0,0,0,0.1);">
                    📦 Без треккинга (${withoutLink.length})
                </div>
                <div style="flex: 1; height: 2px; background: linear-gradient(90deg, var(--border), transparent); border-radius: 2px;"></div>
            </div>`;
            html += withoutLink.map(o => renderCard(o)).join('');
        }

        grid.innerHTML = html;

        // ЗАПУСК ПРОВЕРКИ ЧАСОВ
        document.querySelectorAll('.gps-loading').forEach(icon => {
            const key = icon.getAttribute('data-key');
            if (!key) return;
            fetch(`/api/check_gps/${encodeURIComponent(key)}`)
                .then(res => res.json())
                .then(data => {
                    if (data.valid === true) {
                        icon.innerHTML = '🌍';
                        icon.title = 'GPS доступен';
                    } else if (data.valid === false) {
                        icon.style.display = 'none';
                    } else {
                        icon.innerHTML = '🌍';
                    }
                })
                .catch(() => { icon.style.display = 'none'; });
        });
        
    }catch(e){
        grid.innerHTML='<div style="text-align:center;width:100%;color:red;">Ошибка загрузки</div>';
    }
}

function refreshData(){
    const b=document.getElementById('refreshBtn');
    b.innerHTML='⏳ Загрузка...';
    b.disabled=true;
    loadOrders(true).finally(()=>{
        b.innerHTML='🔄 Обновить';
        b.disabled=false;
    });
}

async function openOrder(o){
    document.getElementById('modal').classList.add('open');
    const st=(o.status||'').toLowerCase();
    const isDelivered=(st==='завершено'||st==='выгружено');
    
    const dates=isDelivered
        ?`<div><strong>Дата отправки</strong>${o.ship_date||'—'}</div><div><strong>Прибытие (план)</strong>${o.plan_arrival||'—'}</div><div><strong>Дата выгрузки</strong>${o.arrival_date||'нет данных'}</div>`
        :`<div><strong>Дата отправки</strong>${o.ship_date||'—'}</div><div><strong>Прибытие (план)</strong>${o.plan_arrival||'—'}</div>`;
    
    document.getElementById('modalContent').innerHTML=`<h2>📦 ${o.order_id} <small style="color:var(--text-sec)">(${o.year})</small></h2>
    <div class="modal-info">
        <div><strong>Адрес</strong>${o.address||'—'}</div>
        <div><strong>Перевозчик</strong>${o.carrier||'—'}</div>
        <div><strong>Машина (ТС)</strong>${o.vehicle||'—'}</div>
        <div><strong>Водитель</strong>${o.driver||'—'}</div>
        <div><strong>Вес</strong>${o.weight?o.weight+' кг':'—'}</div>
        <div><strong>Статус</strong>${o.status||'—'}</div>
        ${dates}
    </div>
    ${o.comment?`<div style="margin-top:20px;padding:12px;background:rgba(0,0,0,.05);border-radius:8px;"><strong>Примечание:</strong> ${o.comment}</div>`:''}
    
    <div id="map"><img src="/static/logo-2.png" class="custom-spinner" alt="Загрузка..."><div>Загрузка координат...</div></div>`;
    
    if(o.current_lat&&o.current_lng){
        renderMap(o.current_lat,o.current_lng);
    }else if(o.gps_link){
        loadCoords(o._key,o.gps_link);
    }else{
        document.getElementById('map').innerHTML='<div style="text-align:center;padding:50px;color:#888;">GPS отсутствует или ссылка недействительна.</div>';
    }
}

async function loadCoords(key,link){
    const m=document.getElementById('map');
    m.innerHTML='<img src="/static/logo-2.png" class="custom-spinner" alt="Загрузка..."><div>Запрос к спутнику...</div>';
    try{
        const r=await fetch(`/api/coords/${encodeURIComponent(key)}`);
        if(r.ok){
            const d=await r.json();
            renderMap(d.lat,d.lng);
        }else{
            const e=await r.json();
            m.innerHTML=`<div style="text-align:center;padding:20px;color:#888;">${e.detail||'GPS недоступен'}<br><br><a href="${link}" target="_blank" class="btn-reload">🗺️ Открыть карту BaltGPS</a></div>`;
        }
    }catch(e){
        m.innerHTML=`<div style="text-align:center;padding:20px;color:#888;">Ошибка сети<br><br><a href="${link}" target="_blank" class="btn-reload">🗺️ Открыть карту BaltGPS</a></div>`;
    }
}

function renderMap(lat,lng){
    const m=document.getElementById('map');
    m.innerHTML='';
    if(map)map.remove();
    map=L.map('map').setView([lat,lng],8);
    L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png').addTo(map);
    L.marker([lat,lng]).addTo(map);
}

function closeModal(){
    document.getElementById('modal').classList.remove('open');
    if(map){map.remove();map=null;}
}

loadTabs().then(loadYears);
</script>
</body></html>"""


ADMIN_HTML = r"""<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8"><title>Админ-панель</title>
<style>
body{font-family:Arial;padding:20px;background:#f0f2f5;margin:0}
.top-bar{display:flex;justify-content:space-between;align-items:center;margin-bottom:20px}
.btn{padding:10px 20px;border:none;border-radius:6px;cursor:pointer;font-weight:bold}
.btn-green{background:#28a745;color:#fff}.btn-blue{background:#007bff;color:#fff}.btn-red{background:#dc3545;color:#fff}
.panel{background:#fff;padding:20px;border-radius:8px;box-shadow:0 2px 8px rgba(0,0,0,.1);margin-bottom:20px}
.panel h3{margin-top:0}
.upload-box{display:flex;gap:10px;align-items:center;flex-wrap:wrap}
.settings-grid{display:flex;gap:20px;margin-top:10px}
.settings-grid label{display:flex;align-items:center;gap:6px;cursor:pointer}
table{border-collapse:collapse;width:100%;background:#fff;font-size:13px;box-shadow:0 2px 8px rgba(0,0,0,.1)}
th,td{border:1px solid #ddd;padding:8px;text-align:left}
th{background:#333;color:#fff;position:sticky;top:0}
tr:hover{background:#f0f8ff}
#filter{padding:10px;width:300px;margin-bottom:10px;border:1px solid #ccc;border-radius:6px}
#modal{display:none;position:fixed;top:0;left:0;width:100%;height:100%;background:rgba(0,0,0,.5);z-index:1000}
#modalContent{background:#fff;max-width:640px;margin:60px auto;padding:30px;border-radius:8px;max-height:80vh;overflow-y:auto}
#modalContent label{display:block;margin-top:10px;font-weight:bold}
#modalContent input,#modalContent select{width:100%;padding:8px;box-sizing:border-box;margin-top:4px}
.modal-buttons{margin-top:20px;text-align:right}
#uploadStatus{font-weight:bold;margin-left:10px}
</style>
</head>
<body>
<div class="top-bar"><h1>🚚 Управление перевозками</h1><button class="btn btn-green" onclick="openAdd()">➕ Добавить заказ</button></div>

<div class="panel">
<h3>👁️ Видимость вкладок на главной</h3>
<div class="settings-grid">
<label><input type="checkbox" id="chk_in_transit" onchange="saveTabs()"> 🚚 В пути</label>
<label><input type="checkbox" id="chk_delivered" onchange="saveTabs()"> ✅ Прибывшие</label>
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
<p style="font-size:12px;color:#666;margin-top:10px;">Берётся лист <b>"Исх.логистика"</b>. Фильтры/скрытые строки снимаются автоматически. Записи сливаются по ключу <code>order_id__год</code>, история сохраняется, прошлые годы уходят в архив (не удаляются).</p>
</div>

<input id="filter" placeholder="Поиск по номеру, адресу, машине..." oninput="renderTable()"/>
<table id="shipments">
<thead><tr><th>Заказ</th><th>Год</th><th>Адрес</th><th>Перевозчик</th><th>Машина</th><th>Статус</th><th>Отгрузка</th><th>Прибытие</th><th>Действия</th></tr></thead>
<tbody></tbody>
</table>

<div id="modal"><div id="modalContent">
<h2 id="modalTitle">Добавить заказ</h2>
<label>Номер заказа *</label><input id="f_order_id"/>
<label>Направление</label><input id="f_direction"/>
<label>Адрес доставки</label><input id="f_address"/>
<label>Вес</label><input id="f_weight" type="number"/>
<label>Дата отправки</label><input id="f_ship_date" type="date"/>
<label>План прибытия</label><input id="f_plan_arrival" type="date"/>
<label>Дата выгрузки</label><input id="f_arrival_date" type="date"/>
<label>Перевозчик</label><input id="f_carrier"/>
<label>Машина</label><input id="f_vehicle"/>
<label>Водитель</label><input id="f_driver"/>
<label>GPS-ссылка</label><input id="f_gps_link"/>
<label>Примечание</label><input id="f_comment"/>
<label>Статус</label>
<select id="f_status">
<option value="планируется">планируется</option>
<option value="в пути">в пути</option>
<option value="выгружено">выгружено</option>
<option value="завершено">завершено</option>
</select>
<div class="modal-buttons">
<button class="btn" onclick="closeModal()">Отмена</button>
<button class="btn btn-green" onclick="saveOrder()">Сохранить</button>
</div>
</div></div>

<script>
let allData=[];
async function loadSettings(){const r=await fetch('/api/settings/tabs');const s=await r.json();document.getElementById('chk_in_transit').checked=s.show_in_transit!==false;document.getElementById('chk_delivered').checked=s.show_delivered!==false;document.getElementById('chk_with_gps').checked=s.show_with_gps!==false;}
async function saveTabs(){const p={show_in_transit:document.getElementById('chk_in_transit').checked,show_delivered:document.getElementById('chk_delivered').checked,show_with_gps:document.getElementById('chk_with_gps').checked};await fetch('/api/admin/settings/tabs',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(p)});}
async function load(){const r=await fetch('/api/admin/shipments');allData=await r.json();renderTable();}
function renderTable(){const q=document.getElementById('filter').value.toLowerCase();const f=allData.filter(s=>[(s.order_id||''),(s.address||''),(s.vehicle||''),(s.carrier||'')].join(' ').toLowerCase().includes(q));document.querySelector('#shipments tbody').innerHTML=f.map(s=>`<tr>
<td><b>${s.order_id}</b>${s.is_service_row?' <small>(служ.)</small>':''}</td>
<td>${s.year||''}</td>
<td>${s.address||''}</td>
<td>${s.carrier||''}</td>
<td>${s.vehicle||''}</td>
<td>${s.status||'—'}</td>
<td>${s.ship_date||''}</td>
<td>${s.arrival_date||s.plan_arrival||''}</td>
<td><button class="btn btn-blue" style="padding:4px 8px;" onclick='openEdit(${JSON.stringify(s).replace(/'/g,"&#39;")})'>✏️</button>
<button class="btn btn-red" style="padding:4px 8px;" onclick="del('${s._key}')">🗑</button></td></tr>`).join('');}
function openAdd(){['order_id','direction','address','weight','ship_date','plan_arrival','arrival_date','carrier','vehicle','driver','gps_link','comment'].forEach(f=>document.getElementById('f_'+f).value='');document.getElementById('f_order_id').disabled=false;document.getElementById('f_status').value='планируется';document.getElementById('modalTitle').innerText='Добавить заказ';document.getElementById('modal').style.display='block';}
function openEdit(s){['order_id','direction','address','weight','ship_date','plan_arrival','arrival_date','carrier','vehicle','driver','gps_link','comment'].forEach(f=>document.getElementById('f_'+f).value=s[f]||'');document.getElementById('f_order_id').disabled=true;document.getElementById('f_status').value=s.status||'планируется';document.getElementById('modalTitle').innerText='Редактировать: '+s.order_id+' ('+s.year+')';document.getElementById('modal').style.display='block';}
function closeModal(){document.getElementById('modal').style.display='none';}
async function saveOrder(){const p={order_id:document.getElementById('f_order_id').value,direction:document.getElementById('f_direction').value,address:document.getElementById('f_address').value,weight:parseFloat(document.getElementById('f_weight').value)||null,ship_date:document.getElementById('f_ship_date').value||null,plan_arrival:document.getElementById('f_plan_arrival').value||null,arrival_date:document.getElementById('f_arrival_date').value||null,carrier:document.getElementById('f_carrier').value,vehicle:document.getElementById('f_vehicle').value,driver:document.getElementById('f_driver').value,gps_link:document.getElementById('f_gps_link').value,comment:document.getElementById('f_comment').value,status:document.getElementById('f_status').value};await fetch('/api/admin/shipments',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(p)});closeModal();load();}
async function del(key){if(!confirm('Удалить запись '+key+'?'))return;await fetch('/api/admin/shipments/'+encodeURIComponent(key),{method:'DELETE'});load();}
async function uploadExcel(){const fi=document.getElementById('excelFile');if(!fi.files.length){alert('Выберите файл!');return;}const fd=new FormData();fd.append('file',fi.files[0]);const b=document.getElementById('uploadBtn');const st=document.getElementById('uploadStatus');b.disabled=true;b.innerText='⏳ Обработка...';st.innerText='Пожалуйста, подождите...';st.style.color='blue';try{const r=await fetch('/api/admin/upload',{method:'POST',body:fd});const d=await r.json();if(r.ok){st.innerText='✅ '+d.message;st.style.color='green';load();}else{st.innerText='❌ Ошибка: '+d.detail;st.style.color='red';}}catch(e){st.innerText='❌ Ошибка сети';st.style.color='red';}finally{b.disabled=false;b.innerText='Обновить базу из файла';}}
loadSettings();load();
</script>
</body></html>"""
