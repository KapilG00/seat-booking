from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from app.api.deps import get_db
from app.db.database import Database

router = APIRouter(tags=["health"])


@router.get("/healthz")
async def liveness() -> dict[str, str]:
    """Liveness: the process is up. Deliberately checks no dependencies."""
    return {"status": "ok"}


@router.get("/readyz")
async def readiness(db: Database = Depends(get_db)) -> JSONResponse:
    """Readiness: can we serve traffic? Fails closed if the DB isn't reachable."""
    ok, reason = await db.ping()
    if ok:
        return JSONResponse({"status": "ready", "database": "ok"})
    return JSONResponse({"status": "not_ready", "database": reason}, status_code=503)
