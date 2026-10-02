from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

router = APIRouter(tags=["health"])


@router.get("/healthz")
async def liveness() -> dict[str, str]:
    """Liveness: the process is up. Deliberately checks no dependencies."""
    return {"status": "ok"}


@router.get("/readyz")
async def readiness(request: Request) -> JSONResponse:
    """Readiness: can we serve traffic? Fails closed if the DB isn't reachable."""
    ok, reason = await request.app.state.db.ping()
    if ok:
        return JSONResponse({"status": "ready", "database": "ok"})
    return JSONResponse(
        {"status": "not_ready", "database": reason}, status_code=503
    )
