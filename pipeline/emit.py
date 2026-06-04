"""
Event emission: builds schema-compliant events and posts them to the API.

Kept separate from detection so the same emitter serves both the real CV pipeline
and the synthetic generator. Events are POSTed in batches of <=500 to /events/ingest,
matching the ingest contract.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Optional

import urllib.request
from networkx.algorithms import boundary


def make_event(
    store_id: str,
    camera_id: str,
    visitor_id: str,
    event_type: str,
    ts: datetime,
    *,
    zone_id: Optional[str] = None,
    dwell_ms: int = 0,
    is_staff: bool = False,
    confidence: float = 0.9,
    queue_depth: Optional[int] = None,
    sku_zone: Optional[str] = None,
    session_seq: int = 1,
) -> dict:
    # Deterministic event_id generation to ensure idempotency when run repeatedly/concurrently
    ts_str = ts.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    unique_name = (
        f"{store_id}:{camera_id}:{visitor_id}:{event_type}:"
        f"{ts_str}:{session_seq}:{zone_id}"
    )
    event_id = str(uuid.uuid5(uuid.NAMESPACE_DNS, unique_name))

    return {
        "event_id": event_id,
        "store_id": store_id,
        "camera_id": camera_id,
        "visitor_id": visitor_id,
        "event_type": event_type,
        "timestamp": ts.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "zone_id": zone_id,
        "dwell_ms": dwell_ms,
        "is_staff": is_staff,
        "confidence": round(confidence, 3),
        "metadata": {
            "queue_depth": queue_depth,
            "sku_zone": sku_zone,
            "session_seq": session_seq,
        },
    }


def post_events(api_url: str, events: list[dict]) -> dict:
    """POST a batch to the ingest endpoint. Chunks to <=500 per request."""
    results = {"accepted": 0, "duplicates": 0, "rejected": 0}

    for i in range(0, len(events), 500):
        chunk = events[i : i + 500]

        # Improvement 1: Explicit UTF-8 JSON encoding
        data = json.dumps(
            {"events": chunk},
            ensure_ascii=False,
        ).encode("utf-8")

        req = urllib.request.Request(
            f"{api_url}/events/ingest",
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        try:
            # Improvement 2: Prevent detector crash on API failures
            with urllib.request.urlopen(req, timeout=30) as resp:
                try:
                    # Improvement 3: Handle non-JSON responses safely
                    body = json.loads(resp.read())
                except json.JSONDecodeError:
                    print(
                        f"Error: API returned non-JSON response "
                        f"(HTTP {resp.status})"
                    )
                    continue

        except Exception as e:
            print(f"Error posting batch: {e}")
            continue

        for k in results:
            results[k] += body.get(k, 0)

    return results