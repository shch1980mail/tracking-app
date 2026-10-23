# Tracking App v2

## Запуск
```bash
cp .env.example .env
docker compose up -d --build
docker compose exec app python scripts/import_json.py "shipments (2).json"
