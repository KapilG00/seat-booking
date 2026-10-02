from uuid import UUID

from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import JSONResponse

from app.api.deps import current_user, get_reservation_service
from app.core.errors import DomainError, InvalidRequest
from app.observability.metrics import (
    RESERVATIONS_CANCELLED,
    RESERVATIONS_CONFIRMED,
    RESERVATIONS_DECLINED,
    SEATS_CONFIRMED,
)
from app.observability.middleware import annotate
from app.schemas import ReservationOut, ReserveRequest
from app.services import ReservationService

router = APIRouter(tags=["reservations"])

_DECLINE_RESPONSES = {
    409: {"description": "seat_taken | per_user_limit | idempotency_key_reused"},
    401: {"description": "missing/invalid token"},
}


def _resolve_idempotency_key(header_key: str | None, body_key: str | None) -> str:
    if header_key and body_key and header_key != body_key:
        raise InvalidRequest("Idempotency-Key header and body idempotency_key differ")
    key = header_key or body_key
    if not key:
        raise InvalidRequest("an idempotency key is required (Idempotency-Key header or body)")
    if len(key) > 200:
        raise InvalidRequest("idempotency key too long (max 200)")
    return key


@router.post(
    "/shows/{show_id}/reserve",
    status_code=201,
    response_model=ReservationOut,
    response_model_exclude_none=True,
    responses={200: {"description": "Idempotent replay of an earlier request"}, **_DECLINE_RESPONSES},
)
async def reserve(
    show_id: UUID,
    body: ReserveRequest,
    request: Request,
    user_id: str = Depends(current_user),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    reservations: ReservationService = Depends(get_reservation_service),
):
    """All-or-nothing: either every requested seat is confirmed to the caller,
    or none is (409). A retry with the same key returns the original
    reservation with 200; the same key with different seats is a 409."""
    sid = str(show_id)
    annotate(request.scope, show_id=sid, user_id=user_id, seats=body.seats)
    try:
        key = _resolve_idempotency_key(idempotency_key, body.idempotency_key)
        result = await reservations.reserve(show_id, user_id, body.seats, key)
    except DomainError as exc:
        if exc.metric_reason:
            RESERVATIONS_DECLINED.labels(sid, exc.metric_reason).inc()
        annotate(request.scope, outcome="declined", reason=exc.code)
        raise

    payload = ReservationOut(**result.reservation).model_dump(mode="json", exclude_none=True)
    if result.created:
        RESERVATIONS_CONFIRMED.labels(sid).inc()
        SEATS_CONFIRMED.labels(sid).inc(len(body.seats))
        annotate(request.scope, outcome="confirmed", reservation_id=payload["reservation_id"])
        return JSONResponse(payload, status_code=201)

    RESERVATIONS_DECLINED.labels(sid, "idempotent_replay").inc()
    annotate(request.scope, outcome="replayed", reason="idempotent_replay",
             reservation_id=payload["reservation_id"])
    return JSONResponse(payload, status_code=200, headers={"Idempotent-Replayed": "true"})


@router.post(
    "/reservations/{reservation_id}/cancel",
    response_model=ReservationOut,
    response_model_exclude_none=True,
    responses={404: {"description": "No such reservation for this user"}},
)
async def cancel(
    reservation_id: UUID,
    request: Request,
    user_id: str = Depends(current_user),
    reservations: ReservationService = Depends(get_reservation_service),
):
    reservation, changed = await reservations.cancel(reservation_id, user_id)
    if changed:
        RESERVATIONS_CANCELLED.labels(str(reservation["show_id"])).inc()
    annotate(request.scope, user_id=user_id, outcome="cancelled" if changed else "already_cancelled")
    return reservation


@router.get(
    "/reservations/{reservation_id}",
    response_model=ReservationOut,
    response_model_exclude_none=True,
)
async def get_reservation(
    reservation_id: UUID,
    user_id: str = Depends(current_user),
    reservations: ReservationService = Depends(get_reservation_service),
):
    return await reservations.get(reservation_id, user_id)
