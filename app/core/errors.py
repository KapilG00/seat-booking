"""Domain outcomes that map to clean 4xx responses (never 5xx)."""


class DomainError(Exception):
    status_code = 400
    code = "bad_request"
    # Label for reservations_declined_total; None = not a reservation decline.
    metric_reason: str | None = None

    def __init__(self, message: str, **extra) -> None:
        super().__init__(message)
        self.message = message
        self.extra = extra

    def body(self) -> dict:
        return {"error": self.code, "message": self.message, **self.extra}


class Unauthorized(DomainError):
    status_code = 401
    code = "unauthorized"


class NotFound(DomainError):
    status_code = 404
    code = "not_found"


class InvalidRequest(DomainError):
    status_code = 422
    code = "invalid_request"


class SeatTaken(DomainError):
    status_code = 409
    code = "seat_taken"
    metric_reason = "seat_taken"


class PerUserLimitExceeded(DomainError):
    status_code = 409
    code = "per_user_limit"
    metric_reason = "per_user_limit"


class IdempotencyKeyReused(DomainError):
    status_code = 409
    code = "idempotency_key_reused"
    metric_reason = "idempotency_key_reused"
