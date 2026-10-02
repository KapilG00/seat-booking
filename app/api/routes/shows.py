from uuid import UUID

from fastapi import APIRouter, Depends, Query

from app.api.deps import get_show_service, require_admin
from app.observability.metrics import init_show_labels
from app.schemas import ShowCreate, ShowOut
from app.services import ShowService

router = APIRouter(tags=["shows"])


@router.post(
    "/shows",
    status_code=201,
    response_model=ShowOut,
    response_model_exclude_none=True,
    dependencies=[Depends(require_admin)],
)
async def create_show(body: ShowCreate, shows: ShowService = Depends(get_show_service)) -> dict:
    show = await shows.create(
        name=body.name,
        seats=body.seats,
        price_paise=body.price_paise,
        per_user_limit=body.per_user_limit,
    )
    init_show_labels(str(show["id"]))
    return show


@router.get("/shows/{show_id}", response_model=ShowOut, response_model_exclude_none=True)
async def get_show(
    show_id: UUID,
    include_seats: bool = Query(True, alias="seats", description="Set seats=false for counts only"),
    shows: ShowService = Depends(get_show_service),
) -> dict:
    return await shows.get(show_id, include_seats)
