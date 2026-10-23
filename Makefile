.PHONY: up down logs import test lint

up:
	docker compose up -d --build

down:
	docker compose down

logs:
	docker compose logs -f app

import:
	docker compose exec app python scripts/import_json.py "shipments (2).json"

test:
	docker compose exec app pytest -v

test-local:
	pytest -v
