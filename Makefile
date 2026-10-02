URL ?= http://localhost:8000
ADMIN_TOKEN ?= dev-admin-token
# Extra flags for the burst script, e.g. ARGS="--requests 5000 --concurrency 200"
ARGS ?=

.PHONY: help db up down dev test burst logs migrate revision db-check

help:
	@echo "make db      - start Postgres only (for local dev/tests)"
	@echo "make up      - build and run app + Postgres in Docker"
	@echo "make down    - stop containers"
	@echo "make dev     - run the API with autoreload against local Postgres"
	@echo "make test    - run the test suite (needs 'make db')"
	@echo "make burst URL=https://... ADMIN_TOKEN=... - on-sale stampede + checks"
	@echo "make logs    - follow app logs from Docker"
	@echo "make migrate - apply migrations (the app also does this on startup)"
	@echo "make revision m='add foo' - new Alembic migration from model changes (review it!)"
	@echo "make db-check - fail if models and the DB schema differ"

db:
	docker compose up -d --wait db

up:
	docker compose up -d --build --wait

down:
	docker compose down

dev: db
	uv run uvicorn app.main:app --reload

test: db
	uv run pytest -q

burst:
	ADMIN_TOKEN=$(ADMIN_TOKEN) uv run scripts/burst.py $(URL) $(ARGS)

logs:
	docker compose logs -f app

migrate:
	uv run alembic upgrade head

revision:
	uv run alembic revision --autogenerate -m "$(m)"

db-check:
	uv run alembic check
