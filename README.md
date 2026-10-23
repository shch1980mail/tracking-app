# Tracking App v2

Сервис отслеживания отправлений: веб-интерфейс + фоновый парсер GPS-ссылок
(axenta.cloud, gs.baltgps.ru, maps.baltgps.ru).

## Возможности

- 🔍 Поиск по последним 4 цифрам номера, фильтр по статусу, пагинация
- 🗺 Ссылка на Яндекс.Карты по текущим координатам
- ⏱ Фоновый парсер каждые N секунд (настраивается)
- 🔁 Retry с exponential backoff, счётчик неудач, авто-завершение после N провалов
- 🧠 TTL-кэш SID для maps.baltgps.ru (Playwright не дёргается на каждый заказ)
- 🗄 Миграции SQL + `schema_migrations`
- 🐳 Docker + docker compose + healthcheck
- ✅ Тесты (pytest, pytest-asyncio)
- 📊 Структурированные JSON-логи (structlog)

## Быстрый старт

```bash
cp .env.example .env
docker compose up -d --build
docker compose exec app python scripts/import_json.py "shipments (2).json"
open http://localhost:8000
