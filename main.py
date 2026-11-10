from fastapi import FastAPI, HTTPException, Depends, UploadFile, File
from fastapi.responses import HTMLResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from typing import Optional
from contextlib import asynccontextmanager
import os, json, re, requests, asyncio, io, datetime as dt, hashlib
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
DEFAULT_SETTINGS = {
    "show_in_transit": True,
    "show_delivered": True,
    "show_with_gps": False,
    "show_planned": False,
}

if os.path.exists(SETTINGS_FILE):
    with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
        SETTINGS = json.load(f)
    for k, v in DEFAULT_SETTINGS.items():
        SETTINGS.setdefault(k, v)
else:
    SETTINGS = DEFAULT_SETTINGS.copy()

with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
    json.dump(SETTINGS, f, ensure_ascii=False, indent=2)


def save_settings():
    with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(SETTINGS, f, ensure_ascii=False, indent=2)


DB_FILE = "shipments.json"
DB: dict = {}

# FIX 1: единый лок для DB. Все операции записи/слияния/сохранения — под ним.
DB_LOCK = asyncio.Lock()

# FIX 2: поля, которые «принадлежат» пользователю после ручного редактирования.
# При слиянии с Excel для manual_edit-записей эти поля НЕ перезаписываются.
MANUAL_PROTECTED_FIELDS = (
    "direction", "address", "weight", "ship_date", "plan_arrival", "arrival_date",
    "carrier", "vehicle", "driver", "gps_link", "comment", "status",
    "location_note", "rate", "transit_days", "vehicle_type", "thermologger",
)

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


def row_fingerprint(rec: dict) -> str:
    parts = [
        str(rec.get("address") or "").strip(),
        str(rec.get("ship_date") or "").strip(),
        str(rec.get("plan_arrival") or "").strip(),
        str(rec.get("carrier") or "").strip(),
        str(rec.get("vehicle") or "").strip(),
        str(rec.get("weight") or "").strip(),
    ]
    return "|".join(parts)


def short_hash(s: str) -> str:
    return hashlib.md5(s.encode("utf-8")).hexdigest()[:8]


def make_key(order_id: str, year: int, fp: str = None, service: bool = False) -> str:
    base = f"{order_id}__{year}"
    if fp is not None:
        return f"{base}__{short_hash(fp)}"
    return base


def is_service_id(order_id: str) -> bool:
    oid = (order_id or "").strip()
    return oid.lower() in SERVICE_IDS or not re.match(r"^ТПК\d{8}$", oid)


def is_hidden_status(rec: dict) -> bool:
    if SETTINGS.get("show_planned", False):
        return False
    return norm_status(rec.get("status")) in HIDDEN_STATUSES


def load_db():
    # FIX 3: load_db больше НЕ вызывает remap_legacy_keys/save_db.
    # Мы только читаем и нормализуем в памяти. Запись — только по явному действию
    # (API-изменение, загрузка Excel) под DB_LOCK.
    global DB
    if os.path.exists(DB_FILE):
        with open(DB_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
        DB = migrate_raw_db(raw)
        remap_legacy_keys_in_memory()   # больше не пишет на диск
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


def remap_legacy_keys_in_memory():
    # FIX 4: только в памяти. Не пишет на диск. Не меняет _key у manual_edit-записей
    # и у записей с уже стабильным ключом (__rowNNN или __<hash>). Добавляет алиасы.
    global DB
    new_db = {}
    for key, rec in DB.items():
        # Никогда не трогаем ключ ручных записей — иначе фронт теряет _key.
        if rec.get("manual_edit"):
            new_db[key] = rec
            continue

        fp = row_fingerprint(rec)
        service = rec.get("is_service_row", False)
        is_legacy_row = bool(re.search(r"__row\d+$", key))
        order_id = rec.get("order_id") or key.split("__")[0]
        year = rec.get("year") or year_of(rec)
        base_key = f"{order_id}__{year}"

        if key == base_key and not service and not is_legacy_row:
            new_db[key] = rec
            continue

        if is_legacy_row or service:
            new_key = make_key(order_id, year, fp=fp, service=service)
            if new_key in new_db:
                i = 2
                while f"{new_key}#{i}" in new_db:
                    i += 1
                new_key = f"{new_key}#{i}"
            if new_key != key:
                # FIX 5: сохраняем старый ключ как алиас, чтобы фронт мог найти по нему.
                aliases = set(rec.get("_key_aliases") or [])
                aliases.add(key)
                rec["_key_aliases"] = sorted(aliases)
                rec["_key"] = new_key
            new_db[new_key] = rec
        else:
            new_db[key] = rec

    DB = new_db


def save_db_unlocked():
    # Внутренняя, БЕЗ лока. Вызывать только из-под DB_LOCK.
    with open(DB_FILE, "w", encoding="utf-8") as f:
        json.dump(DB, f, ensure_ascii=False, indent=2)


async def save_db():
    # FIX 6: единственная точка сохранения, всегда под локом.
    async with DB_LOCK:
        save_db_unlocked()


def merge_into_db_unlocked(new_records: list) -> dict:
    # FIX 7: логика слияния без лока (лок берётся снаружи).
    # Для записей, помеченных manual_edit, защищаем ручные поля от Excel.
    global DB
    now = dt.datetime.utcnow().isoformat(timespec="seconds")
    stats = {"added": 0, "updated": 0, "unchanged": 0, "duplicates": 0, "protected": 0}
    for rec in new_records:
        key = rec["_key"]
        if key in DB:
            old = DB[key]

            # Переносим «живые» состояния GPS, чтобы не сбрасывать проверку.
            rec["has_valid_gps"] = old.get("has_valid_gps")
            rec["gps_checked"] = old.get("gps_checked", False)
            rec["current_lat"] = old.get("current_lat")
            rec["current_lng"] = old.get("current_lng")

            # FIX 8: если запись редактировали вручную — не затираем поля Excel'ем.
            if old.get("manual_edit"):
                rec["manual_edit"] = True
                for f in MANUAL_PROTECTED_FIELDS:
                    if f in old:
                        rec[f] = old[f]
                if old.get("_key_aliases"):
                    rec["_key_aliases"] = old["_key_aliases"]
                stats["protected"] += 1

            rec["history"] = old.get("history", []) + [{
                "loaded_at": now,
                "snapshot": {k: old.get(k) for k in old if k not in ("history",)},
            }]
            rec["first_seen_at"] = old.get("first_seen_at", now)

            old_clean = {k: v for k, v in old.items() if k not in ("history", "updated_at", "first_seen_at")}
            new_clean = {k: v for k, v in rec.items() if k not in ("history", "updated_at", "first_seen_at")}

            if old_clean == new_clean:
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
    return stats


# ПАРСИНГ EXCEL — без изменений (кроме импорта утилит)
COLUMN_MAP_NORM = {
    "заказ": "order_id",
    "направление": "direction",
    "адресдоставки": "address",
    "вес": "weight",
    "датаотгрузкифакт": "ship_date",
    "датаприбытия": "plan_arrival",
    "перевозчик": "carrier",
    "машина": "vehicle",
    "водитель": "driver",
    "ставка": "rate",
    "сроктранзита": "transit_days",
    "типтс": "vehicle_type",
    "термологер": "thermologger",
    "примечания": "comment",
    "ссылка": "gps_link",
    "статусдоставки": "status",
    "местоположение": "location_note",
}

REQUIRED_COLS = ["Заказ"]


def norm_col_name(name: str) -> str:
    return re.sub(r"\s+", "", str(name)).lower()


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


def _real_excel_rows(contents: bytes, sheet_name: str) -> list:
    wb = openpyxl.load_workbook(io.BytesIO(contents), read_only=True, data_only=True)
    try:
        ws = wb[sheet_name]
        header_row = None
        rows = []
        for r_idx, row in enumerate(ws.iter_rows(values_only=True), start=1):
            if header_row is None:
                if row and any((str(c).strip() if c is not None else "") == "Заказ" for c in row):
                    header_row = r_idx
                    rows.append((r_idx, list(row)))
                continue
            rows.append((r_idx, list(row)))
        return rows
    finally:
        try:
            wb.close()
        except Exception:
            pass


def parse_excel_to_records(contents: bytes, fallback_year: int = None) -> tuple[list, dict]:
    wb = openpyxl.load_workbook(io.BytesIO(contents))
    if "Исх.логистика" not in wb.sheetnames:
        raise HTTPException(400, f"В файле нет листа 'Исх.логистика'. Листы: {wb.sheetnames}")

    strip_filters(wb)
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)

    df = pd.read_excel(buf, sheet_name="Исх.логистика", header=0, dtype=str, keep_default_na=False)
    real_cols = {norm_col_name(c): c for c in df.columns}

    missing = [c for c in REQUIRED_COLS if norm_col_name(c) not in real_cols]
    if missing:
        raise HTTPException(400, f"Не найдены колонки: {missing}. Есть: {list(df.columns)}")

    real_rows = _real_excel_rows(contents, "Исх.логистика")
    data_rows_with_idx = [(r_idx, row) for r_idx, row in real_rows[1:]]
    real_iter = iter(data_rows_with_idx)

    records = []
    audit = {
        "missing_in_db": [], "stale_in_db": [], "duplicates": {}, "misaligned": [],
        "nonstandard_ids": [], "skipped_empty": 0, "weight_parse_failed": [],
        "date_parse_failed": [], "gps_looks_like_text": [],
        "arrival_fact_missing": [], "hidden_planned": 0,
        "legacy_dup_remapped": [],
    }

    for idx, row in df.iterrows():
        raw_order = _safe_str(row.get(real_cols.get("заказ")))
        if not raw_order:
            audit["skipped_empty"] += 1
            continue
        order_id = raw_order
        service = is_service_id(order_id)
        if service:
            audit["nonstandard_ids"].append(order_id)

        try:
            excel_row_idx, _ = next(real_iter)
        except StopIteration:
            excel_row_idx = int(idx) + 2

        rec = {"order_id": order_id, "is_service_row": service, "row_index": excel_row_idx}
        warnings = []

        for norm_col, field in COLUMN_MAP_NORM.items():
            if norm_col == "заказ":
                continue
            real_col = real_cols.get(norm_col)
            if not real_col:
                rec[field] = None
                continue

            val = row.get(real_col)
            if field == "weight":
                fv = _safe_float(val)
                rec[field] = fv
                if fv is None and _safe_str(val):
                    warnings.append("weight_parse_failed")
                    audit["weight_parse_failed"].append({
                        "order_id": order_id, "row": excel_row_idx, "raw": str(val)
                    })
            elif field in ("ship_date", "plan_arrival"):
                dv = parse_iso_date(val)
                rec[field] = dv
                if dv is None and _safe_str(val):
                    warnings.append(f"{field}_parse_failed")
                    audit["date_parse_failed"].append({
                        "order_id": order_id, "row": excel_row_idx, "field": field, "raw": str(val)
                    })
            elif field == "status":
                rec["status"] = norm_status(_safe_str(val) or "")
            elif field == "rate":
                rec[field] = _safe_float(val)
            elif field == "gps_link":
                s = _safe_str(val)
                if s:
                    if not s.startswith("http"):
                        audit["gps_looks_like_text"].append({
                            "order_id": order_id, "row": excel_row_idx, "raw": s
                        })
                    rec[field] = s
                else:
                    rec[field] = None
            else:
                rec[field] = _safe_str(val)

        if not rec.get("gps_link") and rec.get("location_note"):
            loc = str(rec["location_note"])
            m = re.search(r'https?://\S+', loc, re.IGNORECASE)
            if m:
                rec["gps_link"] = m.group(0).strip()
                rec["location_note"] = loc.replace(rec["gps_link"], "").strip() or None

        if rec.get("gps_link"):
            rec["gps_link"] = rec["gps_link"].strip()

        arrival_fact = None
        if norm_status(rec.get("status")) in DELIVERED:
            if "Дата выгрузки (ФАКТ)" in df.columns:
                arrival_fact = parse_iso_date(row.get("Дата выгрузки (ФАКТ)"))
            if not arrival_fact:
                blob = " ".join(str(rec.get(k) or "") for k in ("comment", "location_note"))
                m = re.search(
                    r"(?:выгруж\w*|прибыл\w*|на выгрузке)\D{0,20}"
                    r"(\d{2})[./](\d{2})[./](\d{2,4})",
                    blob, re.IGNORECASE,
                )
                if m:
                    d, mo, y = m.group(1), m.group(2), m.group(3)
                    if len(y) == 2:
                        y = "20" + y
                    arrival_fact = f"{y}-{mo}-{d}"
        rec["arrival_date"] = arrival_fact
        rec["arrival_fact_missing"] = bool(
            norm_status(rec.get("status")) in DELIVERED and not arrival_fact
        )
        if rec["arrival_fact_missing"]:
            audit["arrival_fact_missing"].append({
                "order_id": order_id, "row": excel_row_idx
            })

        rec["year"] = year_of(rec, fallback=fallback_year)
        rec["_row_fp"] = row_fingerprint(rec)
        rec["_key"] = make_key(order_id, rec["year"], service=service)
        rec["data_warnings"] = warnings
        rec["archived"] = False

        rec["has_valid_gps"] = None
        rec["gps_checked"] = False
        rec["current_lat"] = None
        rec["current_lng"] = None

        records.append(rec)

    seen: dict[str, int] = {}
    for r in records:
        base = r["_key"]
        if base in seen:
            order_id = r["order_id"]
            year = r["year"]
            r["_key"] = make_key(order_id, year, fp=r["_row_fp"], service=r["is_service_row"])
            if r["_key"] in seen:
                audit["duplicates"][r["_key"]] = audit["duplicates"].get(r["_key"], 1) + 1
                r["data_warnings"].append("duplicate_order_id_year")
            else:
                seen[r["_key"]] = 1
        else:
            seen[base] = 1

    return records, audit


# ============================================================
# LIFESPAN / APP
# ============================================================
@asynccontextmanager
async def lifespan(app: FastAPI):
    global DB
    load_db()
    # FIX 9: не запускаем update_all_coords сразу, чтобы не гонять с запросами
    # в первые секунды. Дадим приложению подняться.
    if DB:
        async def _delayed_update():
            await asyncio.sleep(10)
            try:
                await update_all_coords()
            except Exception as e:
                print(f"⚠️ init update_all_coords: {e}")
        asyncio.create_task(_delayed_update())
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
    # FIX 10: работаем по снапшоту. Слияние/сохранение — под локом.
    snapshot = list(DB.values())
    for rec in snapshot:
        if rec.get("has_valid_gps") is False:
            rec["current_lat"] = None
            rec["current_lng"] = None

    tasks = [
        fetch_single_order_coords(r)
        for r in snapshot
        if r.get("gps_link") and "baltgps" in r["gps_link"] and not r.get("gps_checked")
    ]
    sem = asyncio.Semaphore(10)

    async def _limited(coro):
        async with sem:
            await coro

    await asyncio.gather(*(_limited(t) for t in tasks))
    await save_db()


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
        "data_warnings": r.get("data_warnings", []),
        "manual_edit": r.get("manual_edit", False),
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
        if s in HIDDEN_STATUSES and not SETTINGS.get("show_planned", False):
            continue
        if status == "in_transit" and s not in IN_TRANSIT:
            continue
        if status == "delivered" and s not in DELIVERED:
            continue
        if status == "with_gps" and not r.get("current_lat"):
            continue
        if status == "planned" and s not in HIDDEN_STATUSES:
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


def _resolve_key(key: str) -> Optional[str]:
    # FIX 11: умеем находить запись по старому ключу-алиасу.
    if key in DB:
        return key
    for k, r in DB.items():
        if key in (r.get("_key_aliases") or []):
            return k
    return None


@app.get("/api/shipments/{order_id}")
async def get_shipments_by_order(order_id: str):
    items = find_all_by_order_id(order_id)
    if not items:
        raise HTTPException(404, "Заказ не найден")
    items.sort(key=lambda r: (r.get("year") or 0, r.get("ship_date") or ""))
    return [public_view(r) for r in items]


@app.get("/api/coords/{key}")
async def get_online_coords(key: str):
    real_key = _resolve_key(key)
    if not real_key:
        raise HTTPException(404, "Заказ не найден")
    r = DB[real_key]
    if r.get("gps_link"):
        lat, lng, status = get_coords_from_baltgps(r["gps_link"])
        if lat and lng:
            r["current_lat"], r["current_lng"], r["has_valid_gps"] = lat, lng, True
            r["gps_checked"] = True
            await save_db()
            return {"lat": lat, "lng": lng}
        if status == "invalid_token":
            r["has_valid_gps"] = False
            r["gps_checked"] = True
            await save_db()
            raise HTTPException(400, "Ссылка на GPS устарела")
        if status == "error":
            raise HTTPException(503, "Спутник не отвечает")

    if r.get("current_lat"):
        return {"lat": r["current_lat"], "lng": r["current_lng"]}
    raise HTTPException(404, "GPS недоступен")


@app.get("/api/check_gps/{key}")
async def check_gps_link(key: str):
    real_key = _resolve_key(key)
    if not real_key:
        raise HTTPException(404, "Заказ не найден")
    r = DB[real_key]
    if not r.get("gps_link"):
        return {"valid": False}

    if r.get("gps_checked"):
        return {"valid": r.get("has_valid_gps")}

    loop = asyncio.get_event_loop()
    lat, lng, status = await loop.run_in_executor(None, get_coords_from_baltgps, r["gps_link"])

    if lat and lng:
        r["current_lat"], r["current_lng"], r["has_valid_gps"] = lat, lng, True
        r["gps_checked"] = True
        await save_db()
        return {"valid": True}
    elif status in ("invalid_token", "no_units"):
        r["has_valid_gps"] = False
        r["gps_checked"] = True
        await save_db()
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
    fp = row_fingerprint(rec)
    rec["_row_fp"] = fp
    base_key = make_key(rec["order_id"], rec["year"], service=service)
    rec["_key"] = base_key
    if rec["_key"] in DB:
        rec["_key"] = make_key(rec["order_id"], rec["year"], fp=fp, service=service)
        if rec["_key"] in DB:
            rec["_key"] = f"{rec['_key']}#{int(dt.datetime.utcnow().timestamp())}"
    rec["has_valid_gps"] = None
    rec["gps_checked"] = False
    # FIX 12: помечаем как ручную правку — Excel её не затрёт, а ключ не будет ремапиться.
    rec["manual_edit"] = True
    rec["manual_edit_at"] = dt.datetime.utcnow().isoformat(timespec="seconds")

    async with DB_LOCK:
        merge_into_db_unlocked([rec])
        save_db_unlocked()

    asyncio.create_task(update_all_coords())
    return {"ok": True, "_key": rec["_key"]}


@app.delete("/api/admin/shipments/{key}")
async def admin_delete(key: str, user=Depends(check_admin)):
    real_key = _resolve_key(key)
    async with DB_LOCK:
        if real_key and real_key in DB:
            del DB[real_key]
            save_db_unlocked()
    return {"ok": True}


@app.post("/api/admin/upload")
async def admin_upload_excel(file: UploadFile = File(...), user=Depends(check_admin)):
    try:
        contents = await file.read()
        m = re.search(r"(20\d{2})", file.filename or "")
        fallback_year = int(m.group(1)) if m else None
        records, audit = parse_excel_to_records(contents, fallback_year=fallback_year)

        # FIX 13: merge + save под одним локом → нет гонки с фоновым update_all_coords.
        async with DB_LOCK:
            stats = merge_into_db_unlocked(records)
            new_keys = {r["_key"] for r in records}
            stale = [r["_key"] for k, r in DB.items() if k not in new_keys and not r.get("archived")]
            audit["stale_in_db"] = stale
            audit["hidden_planned"] = sum(
                1 for r in records if norm_status(r.get("status")) in HIDDEN_STATUSES
            )
            save_db_unlocked()

        asyncio.create_task(update_all_coords())

        ts = dt.datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        audit_path = f"upload_audit_{ts}.json"
        with open(audit_path, "w", encoding="utf-8") as f:
            json.dump(audit, f, ensure_ascii=False, indent=2)

        last = [r["order_id"] for r in records[-5:]]

        warn_parts = []
        if audit["weight_parse_failed"]:
            warn_parts.append(
                f"⚠️ вес не распознан: {len(audit['weight_parse_failed'])} "
                f"({', '.join(x['order_id'] for x in audit['weight_parse_failed'][:3])}...)"
            )
        if audit["gps_looks_like_text"]:
            warn_parts.append(f"⚠️ gps_link не URL: {len(audit['gps_looks_like_text'])}")
        if audit["duplicates"]:
            warn_parts.append(f"⚠️ дубли order_id+year: {len(audit['duplicates'])}")
        if audit["date_parse_failed"]:
            warn_parts.append(f"⚠️ даты не распознаны: {len(audit['date_parse_failed'])}")
        if audit["arrival_fact_missing"]:
            warn_parts.append(f"⚠️ нет факт. даты выгрузки: {len(audit['arrival_fact_missing'])}")
        if audit["hidden_planned"]:
            warn_parts.append(f"⚠️ скрыто в UI (планируется): {audit['hidden_planned']}")
        if stats.get("protected"):
            warn_parts.append(f"🛡️ защищено ручных правок: {stats['protected']}")

        message = (
            f"Загружено {len(records)} строк (добавлено {stats['added']}, "
            f"обновлено {stats['updated']}, без изменений {stats['unchanged']}, "
            f"защищено ручных {stats.get('protected', 0)}). "
            f"Пропущено пустых: {audit['skipped_empty']}. "
            f"Дублей в файле: {len(audit['duplicates'])}. "
            f"Осиротевших в БД: {len(stale)}. "
            f"Последние: {', '.join(last)}. Аудит: {audit_path}"
        )
        if warn_parts:
            message += "\n" + " | ".join(warn_parts)

        return {"ok": True, "message": message}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"Ошибка чтения файла: {str(e)}")


# ============================================================
# ГЛАВНАЯ / АДМИНКА (HTML без изменений — см. ваш файл)
# ============================================================
@app.get("/", response_class=HTMLResponse)
async def user_portal():
    return INDEX_HTML


@app.get("/admin", response_class=HTMLResponse)
async def admin_panel(user=Depends(check_admin)):
    return ADMIN_HTML


# INDEX_HTML и ADMIN_HTML остаются БЕЗ ИЗМЕНЕНИЙ — скопируйте из вашего файла.
# (Ниже — заглушки, чтобы файл был валидным; замените на ваш полный HTML.)
INDEX_HTML = r"""<!DOCTYPE html><html><body>INDEX_HTML — вставьте ваш полный HTML</body></html>"""
ADMIN_HTML = r"""<!DOCTYPE html><html><body>ADMIN_HTML — вставьте ваш полный HTML</body></html>"""
