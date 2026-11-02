import json, os, re, datetime as dt

DB_FILE = "shipments.json"
REPORT = "migration_report.json"

STATUS_CANON = {
    "планируется": "планируется", "в плане": "планируется",
    "в пути": "в пути", "выгружено": "выгружено", "завершено": "завершено",
    "": "планируется", "nan": "планируется",
}

def norm_status(s):
    s = (s or "").strip().lower().replace("ё", "е")
    return STATUS_CANON.get(s, s or "планируется")

def year_of(rec, fallback=None):
    for k in ("ship_date", "plan_arrival", "arrival_date"):
        v = rec.get(k)
        if v and re.match(r"\d{4}", str(v)):
            return int(str(v)[:4])
    return fallback or dt.date.today().year

def migrate():
    if not os.path.exists(DB_FILE):
        print("Нет shipments.json")
        return
    with open(DB_FILE, "r", encoding="utf-8") as f:
        raw = json.load(f)

    out = {}
    report = {"migrated": 0, "duplicates": [], "needs_review": [], "dropped": []}

    # 1) сгруппировать старые записи по order_id
    groups = {}
    for old_key, rec in raw.items():
        oid = rec.get("order_id", old_key)
        groups.setdefault(oid, []).append(rec)

    # 2) для каждой группы — свои ключи по годам
    for oid, recs in groups.items():
        # если в истории уже есть снапшоты — восстановим их как отдельные записи
        expanded = []
        for rec in recs:
            hist = rec.get("history", [])
            expanded.append(rec)
            for h in hist:
                snap = h.get("snapshot") if isinstance(h, dict) else None
                if snap:
                    expanded.append(snap)
        seen_keys = set()
        for rec in expanded:
            year = year_of(rec)
            key = f"{oid}__{year}"
            if key in seen_keys:
                # дубль в одном году — суффикс по дате
                key = f"{oid}__{year}__{rec.get('ship_date') or rec.get('plan_arrival') or 'na'}"
            seen_keys.add(key)
            rec["_key"] = key
            rec["year"] = year
            rec["status"] = norm_status(rec.get("status"))
            rec.setdefault("history", [])
            rec.setdefault("archived", year < dt.date.today().year and rec["status"] in ("завершено", "выгружено"))
            if key in out:
                report["duplicates"].append({"order_id": oid, "key": key})
                i = 2
                while f"{key}#{i}" in out:
                    i += 1
                rec["_key"] = f"{key}#{i}"
            out[rec["_key"]] = rec
            report["migrated"] += 1

    # 3) то, что не удалось развести — в needs_review
    for k, r in list(out.items()):
        if not re.match(r"^[^_]+__\d{4}", k):
            report["needs_review"].append(k)

    with open(DB_FILE, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    with open(REPORT, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    print(f"Мигрировано: {report['migrated']}, дублей: {len(report['duplicates'])}, "
          f"на ручной разбор: {len(report['needs_review'])}. Отчёт: {REPORT}")

if __name__ == "__main__":
    migrate()
