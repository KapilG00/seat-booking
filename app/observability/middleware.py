import re
import time
import uuid

import structlog

from app.observability.metrics import HTTP_LATENCY, HTTP_REQUESTS

logger = structlog.get_logger("access")

_SAFE_REQUEST_ID = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
LOG_EXTRA_KEY = "log_extra"


def annotate(scope: dict, **fields) -> None:
    """Attach fields (outcome, reason, ...) to this request's access-log line."""
    scope.setdefault(LOG_EXTRA_KEY, {}).update(fields)


class RequestContextMiddleware:
    """Pure ASGI middleware: request id, one JSON access-log line, HTTP metrics.

    Honors an incoming X-Request-ID (if well-formed) so a client can correlate
    its own logs with ours, and always echoes the id back in the response.
    """

    def __init__(self, app, access_log: bool = True) -> None:
        self.app = app
        self.access_log = access_log

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        incoming = dict(scope["headers"]).get(b"x-request-id", b"").decode("latin-1")
        request_id = incoming if _SAFE_REQUEST_ID.match(incoming) else uuid.uuid4().hex
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(request_id=request_id)

        status = 500
        start = time.perf_counter()

        async def send_with_request_id(message):
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                headers = list(message.get("headers", []))
                headers.append((b"x-request-id", request_id.encode()))
                message["headers"] = headers
            await send(message)

        try:
            await self.app(scope, receive, send_with_request_id)
        finally:
            elapsed = time.perf_counter() - start
            route = scope.get("route")
            route_path = getattr(route, "path", "unmatched")
            HTTP_REQUESTS.labels(scope["method"], route_path, str(status)).inc()
            HTTP_LATENCY.labels(scope["method"], route_path).observe(elapsed)
            if self.access_log and route_path not in ("/metrics", "/healthz"):
                log = logger.error if status >= 500 else logger.info
                log(
                    "request",
                    method=scope["method"],
                    path=scope["path"],
                    route=route_path,
                    status=status,
                    duration_ms=round(elapsed * 1000, 2),
                    **scope.get(LOG_EXTRA_KEY, {}),
                )
            structlog.contextvars.clear_contextvars()
