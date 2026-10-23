import openpyxl, json

wb = openpyxl.load_workbook("Отгрузки 2026.xlsx", data_only=True)
ws = wb["Исх.логистика"]

def to_str(v): return str(v).strip() if v is not None else None
def to_float(v):
    try: return float(v)
    except: return None
def to_date(v): return str(v)[:10] if v else None

shipments = {}
skipped = []

for row in ws.iter_rows(min_row=2, values_only=True):
    order_id = to_str(row[2])
    if not order_id or not order_id.upper().startswith(("ТПК", "ТОР", "ВЫСТАВКА", "ОБОРУДОВАНИЕ", "КАКАО", "РЕМ")): continue
    
    gps_link_raw = to_str(row[18])  # Колонка S: Ссылка
    note_raw = to_str(row[17])      # Колонка R: Примечание
    status_raw = to_str(row[19])    # Колонка T: Статус

    # ЛОГИКА: если ссылки нет, но в примечании есть что-то похожее на URL
    if not gps_link_raw and note_raw and ("http" in note_raw or "locator" in note_raw or "baltgps" in note_raw or "axenta" in note_raw):
        gps_link_raw = note_raw

    shipments[order_id] = {
        "order_id": order_id, 
        "direction": to_str(row[3]), 
        "address": to_str(row[4]),
        "weight": to_float(row[5]), 
        "ship_date": to_date(row[7]), 
        "plan_arrival": to_date(row[8]),
        "carrier": to_str(row[9]), 
        "vehicle": to_str(row[10]), 
        "driver": to_str(row[11]),
        "gps_link": gps_link_raw, 
        "comment": note_raw, 
        "status": status_raw or "",  # Если статус пустой, оставляем пустым
    }

with open("shipments.json", "w", encoding="utf-8") as f:
    json.dump(shipments, f, ensure_ascii=False, indent=2)

print(f"✅ Загружено: {len(shipments)}")
