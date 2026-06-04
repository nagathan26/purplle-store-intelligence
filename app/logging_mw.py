"""
Structured logging.

Every request emits exactly one JSON log line with the fields the spec requires:
trace_id, store_id, endpoint, latency_ms, event_count (ingest only), status_code.

Logs go to stdout so `docker compose logs` and any log shipper pick them up.
"""
from __future__ import annotations

import json
import sys
import time
import uuid

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request


def _log(payload: dict) -> None:
    sys.stdout.write(
        json.dumps(payload, separators=(",", ":")) + "\n"
    )
    sys.stdout.flush()


class StructuredLoggingMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        trace_id = request.headers.get(
            "x-trace-id",
            str(uuid.uuid4()),
        )

        start = time.perf_counter()

        request.state.trace_id = trace_id

        # Endpoints can populate these values.
        request.state.event_count = None
        request.state.store_id = None

        status_code = 500

        try:
            response = await call_next(request)

            status_code = response.status_code

            # Return trace ID to caller.
            response.headers["x-trace-id"] = trace_id

            return response

        finally:
            # Prefer store_id explicitly provided by endpoint.
            store_id = getattr(
                request.state,
                "store_id",
                None,
            )

            # Fallback to URL parsing for store-specific endpoints.
            if store_id is None:
                parts = request.url.path.strip("/").split("/")

                if (
                    len(parts) >= 2
                    and parts[0] == "stores"
                ):
                    store_id = parts[1]

            payload = {
                "trace_id": trace_id,
                "store_id": store_id,
                "endpoint": request.url.path,
                "method": request.method,
                "latency_ms": round(
                    (time.perf_counter() - start) * 1000,
                    2,
                ),
                "event_count": getattr(
                    request.state,
                    "event_count",
                    None,
                ),
                "status_code": status_code,
            }

            # Logging must never break request handling.
            try:
                _log(payload)
            except Exception:
                pass