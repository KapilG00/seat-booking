# seat-booking

A small JSON API that sells assigned seats for an event and stays correct under on-sale contention.

## Run locally

```bash
uv sync
uv run uvicorn app.main:app --reload
```

## Tests

```bash
uv run pytest
```
