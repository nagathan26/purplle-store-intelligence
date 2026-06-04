"""
Ingestion: validate -> dedup -> store, with partial success.

A batch may contain malformed records. We never fail the whole batch for one bad
event: each record is validated independently, valid ones are stored, and a
structured per-record error list is returned alongside the counts. Idempotency is
guaranteed by the storage layer keying on event_id.
"""
from __future__ import annotations

from pydantic import ValidationError

from .models import Event, IngestRecordError, IngestResponse
from .storage import Store


def ingest_batch(store: Store, raw_events: list[dict]) -> IngestResponse:
    accepted = 0
    duplicates = 0
    rejected = 0

    errors: list[IngestRecordError] = []

    for i, raw in enumerate(raw_events):
        try:
            event = Event.model_validate(raw)

        except ValidationError as exc:
            rejected += 1

            first = exc.errors()[0]

            loc = ".".join(
                str(p)
                for p in first.get("loc", [])
            )

            errors.append(
                IngestRecordError(
                    index=i,
                    event_id=(
                        raw.get("event_id")
                        if isinstance(raw, dict)
                        else None
                    ),
                    error=(
                        f"{loc}: "
                        f"{first.get('msg', 'invalid')}"
                    ),
                )
            )

            continue

        result = store.insert_event(event)

        if result == "inserted":
            accepted += 1

        elif result == "duplicate":
            duplicates += 1

        else:
            raise RuntimeError(
                f"Unexpected insert_event() result: {result!r}"
            )

    return IngestResponse(
        accepted=accepted,
        duplicates=duplicates,
        rejected=rejected,
        errors=errors,
    )