from fastapi import FastAPI, HTTPException, Depends, UploadFile, File
from fastapi.responses import HTMLResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel
from typing import Optional
import os, json, requests, asyncio, io
import pandas as pd

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

def save_db():
    with open("shipments.json", "w", encoding="utf-8") as f:
        json.dump(DB, f, ensure_ascii=False, indent=2)

# === ФУНКЦИЯ ПАРСИНГА GPS (ПРОВЕРКА ВАЛИДНОСТИ) ===
def get_coords_from_baltgps(gps_link: str):
    if not gps_link or "maps.baltgps.ru" not in gps_link:
        return None, None, "no_link"
    try:
        token = gps_link.split("?t=")[-1]
        r = requests.get("https://maps.baltgps.ru/wialon/ajax.html", params={
            "svc": "token/login", "params": json.dumps({"token": token})
        }, timeout=(3.05, 5))
        
        data = r.json()
        if "error" in data:
            return None, None, "invalid_token"
            
        if "token" not in data: return None, None, "invalid_token"
        
        unit_info = json.loads(data["token"])
        items = unit_info.get("items", [])
        if not items: return None, None, "no_units"
        unit_id = items[0]
        sid = data.get("eid")
        
        r2 = requests.get("https://maps.baltgps.ru/wialon/ajax.html", params={
            "svc": "core/search_item", "params": json.dumps({"id": unit_id, "flags": 1025}), "sid": sid
        }, timeout=(3.05, 5))
        
        pos = r2.json().get("item", {}).get("pos", {})
        if pos and "x" in pos and "y" in pos:
            return float(pos["y"]), float(pos["x"]), "ok"
        return None, None, "no_data"
    except Exception as e:
        return None, None, "error"

async def fetch_single_order_coords(order_id, o):
    loop = asyncio.get_event_loop()
    lat, lng, status = await loop.run_in_executor(None, get_coords_from_baltgps, o["gps_link"])
    if lat and lng:
        o["current_lat"] = lat
        o["current_lng"] = lng
        o["has_valid_gps"] = True
    else:
        o["has_valid_gps"] = False
        # Если ссылка битая, мы не удаляем её сразу, но помечаем как невалидную
        if status in ["invalid_token", "error"]:
            o["gps_status"] = "invalid"
        else:
            o["gps_status"] = status

async def update_all_coords():
    tasks = []
    for order_id, o in DB.items():
        if o.get("gps_link") and not o.get("current_lat"):
            tasks.append(fetch_single_order_coords(order_id, o))
    semaphore = asyncio.Semaphore(10)
    async def limited_task(task):
        async with semaphore:
            await task
    await asyncio.gather(*(limited_task(t) for t in tasks))
    save_db()

@app.on_event("startup")
async def startup():
    global DB
    if os.path.exists("shipments.json"):
        with open("shipments.json", "r", encoding="utf-8") as f:
            DB = json.load(f)
        asyncio.create_task(update_all_coords())

def check_admin(credentials: HTTPBasicCredentials = Depends(security)):
    if credentials.username != ADMIN_USER or credentials.password != ADMIN_PASS:
        raise HTTPException(401, "Неверный логин/пароль")
    return credentials.username

# === API ДЛЯ ПОЛЬЗОВАТЕЛЕЙ ===
@app.get("/api/shipments")
async def list_shipments(status: Optional[str] = None, search: Optional[str] = None, sort_by: str = "gps"):
    result = []
    for o in DB.values():
        s = str(o.get("status", "")).lower()
        
        # Строгая фильтрация
        if status == "in_transit" and s not in ['в пути', 'планируется']: continue
        if status == "delivered" and s not in ['завершено', 'выгружено']: continue
        
        # Вкладка "С GPS" - ТОЛЬКО ВАЛИДНЫЕ
        if status == "with_gps":
            if not o.get("gps_link") or o.get("has_valid_gps") is False or not o.get("current_lat"):
                continue
        
        if search:
            q = search.lower()
            if q not in o.get("order_id", "").lower() and q not in o.get("address", "").lower():
                continue
        result.append(o)
    
    def sort_key(x):
        # Признак валидности: 1 если есть координаты ИЛИ has_valid_gps != False
        is_valid = 1 if (x.get("current_lat") or x.get("has_valid_gps") is True) else 0
        has_link = 1 if (x.get("gps_link") and x.get("has_valid_gps") is not False) else 0
        
        order_id = str(x.get("order_id") or "")
        city = str(x.get("address") or "").lower()
        
        if sort_by == "gps":
            # Сначала с валидным GPS, потом без. Внутри группы по номеру.
            return (not is_valid, order_id)
        elif sort_by == "order_id":
            return order_id
        elif sort_by == "city":
            return (city == "", city, order_id)
        return (not is_valid, order_id)

    result.sort(key=sort_key)
    return result

@app.get("/api/coords/{order_id}")
async def get_online_coords(order_id: str):
    if order_id not in DB: raise HTTPException(404, "Заказ не найден")
    o = DB[order_id]
    
    # Если координаты есть - отдаем сразу
    if o.get("current_lat"): 
        return {"lat": o["current_lat"], "lng": o["current_lng"]}
        
    # Если ссылка есть, но координат нет - пробуем онлайн
    if o.get("gps_link"):
        lat, lng, status = get_coords_from_baltgps(o["gps_link"])
        if lat and lng:
            o["current_lat"] = lat
            o["current_lng"] = lng
            o["has_valid_gps"] = True
            return {"lat": lat, "lng": lng}
        else:
            o["has_valid_gps"] = False
            if status in ["invalid_token", "error"]:
                o["gps_status"] = "invalid"
            save_db()
            
    raise HTTPException(404, "GPS недоступен")

# === АДМИНКА ===
@app.get("/api/admin/shipments")
async def admin_list(user=Depends(check_admin)):
    return list(DB.values())

@app.post("/api/admin/shipments")
async def admin_create(s: Shipment, user=Depends(check_admin)):
    DB[s.order_id] = s.model_dump()
    # Сбрасываем статус валидности для нового/измененного заказа
    if DB[s.order_id].get("gps_link"):
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
        df = pd.read_excel(io.BytesIO(contents), sheet_name="Исх.логистика")
        df = df.dropna(subset=['Заказ'])
        
        new_db = {}
        for _, row in df.iterrows():
            order_id = str(row['Заказ']).strip()
            def safe_float(val):
                try: return float(str(val).replace(',', '.')) if not pd.isna(val) else None
                except: return None
            def fmt_date(val):
                try: return pd.to_datetime(val).strftime('%Y-%m-%d') if not pd.isna(val) else None
                except: return str(val)[:10]

            gps = str(row.get('ссылка', '')) if not pd.isna(row.get('ссылка')) else None
            
            new_db[order_id] = {
                "order_id": order_id,
                "address": str(row.get('Адрес доставки', '')) if not pd.isna(row.get('Адрес доставки')) else None,
                "weight": safe_float(row.get('вес')),
                "plan_arrival": fmt_date(row.get('Дата прибытия')),
                "carrier": str(row.get('Перевозчик', '')) if not pd.isna(row.get('Перевозчик')) else None,
                "vehicle": str(row.get('Машина', '')) if not pd.isna(row.get('Машина')) else None,
                "driver": str(row.get('Водитель', '')) if not pd.isna(row.get('Водитель')) else None,
                "gps_link": gps,
                "comment": str(row.get('Примечания', '')) if not pd.isna(row.get('Примечания')) else None,
                "status": str(row.get('статус доставки', '')).strip().lower() if not pd.isna(row.get('статус доставки')) else "",
                "has_valid_gps": None # Сбрасываем, чтобы сервер заново проверил
            }
        DB = new_db
        save_db()
        asyncio.create_task(update_all_coords())
        return {"ok": True, "message": f"Загружено {len(DB)} заказов. Идет проверка GPS..."}
    except Exception as e:
        raise HTTPException(500, f"Ошибка чтения файла: {str(e)}")

@app.get("/api/admin/report")
async def get_report(user=Depends(check_admin)):
    in_transit = [o for o in DB.values() if str(o.get("status", "")).lower() in ['в пути', 'планируется']]
    # В отчете тоже используем has_valid_gps
    with_gps = [o for o in in_transit if o.get("gps_link") and o.get("has_valid_gps") is not False and o.get("current_lat")]
    without_gps = [o for o in in_transit if o not in with_gps]
    return {"total": len(in_transit), "with_gps": with_gps, "without_gps": without_gps}
