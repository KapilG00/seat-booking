from fastapi import APIRouter, Depends, Response

from app.api.deps import get_db
from app.db.database import Database
from app.observability.metrics import refresh_db_gauges, render_latest

router = APIRouter(tags=["observability"])


@router.get("/metrics", include_in_schema=False)
async def metrics(db: Database = Depends(get_db)) -> Response:
    await refresh_db_gauges(db)
    body, content_type = render_latest()
    return Response(body, media_type=content_type)
